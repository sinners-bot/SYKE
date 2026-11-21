"""More ways to earn on THE STRIP: a weekly reward, job shifts with mini-games, and a stock market.

* `weekly`: a big payout once a week. Claim every week and it grows; skip a week and the streak is gone.
* `jobs` / `job`: pick a job (bartender, valet, card dealer, bouncer, DJ) and work shifts. Each shift is a
  quick mini-game; get it right and fast for full pay. Better jobs unlock as you work more shifts.
* `stocks`: six made-up companies whose prices move with what actually happens in the server: chat
  volume, laughs, voice, casino action, late-night chatter, deleted messages. Buy low, sell high.

Everything pays in Strip cash and needs the economy to be open (`economy on`). Money moves through the
same storage calls as the rest of the Strip, so the game, the dashboard and the bot all agree.
"""

from __future__ import annotations

import math
import random
import re
import time
from datetime import datetime

import discord
from discord.ext import commands, tasks

from .moderation import BRAND, OK, WARN, embed

# Weekly

WEEK_BASE = 2_000
WEEK_STEP = 500
WEEK_CAP = 8
WEEK_COOLDOWN = 6.5 * 86400
WEEK_WINDOW = 14 * 86400


def weekly_reward(streak: int) -> int:
    return WEEK_BASE + min(max(streak, 1) - 1, WEEK_CAP) * WEEK_STEP


def next_week_streak(last_at: float, now: float, streak: int) -> int:
    return max(1, streak) + 1 if last_at and now - last_at <= WEEK_WINDOW else 1


# Jobs

JOB_COOLDOWN = 45 * 60
JOB_SECONDS = 20
JOBS = {
    # key: (title, icon, unlock after N shifts, low pay, high pay, what the shift is)
    "valet": ("Valet", "🚗", 0, 140, 320, "Find the right car key"),
    "bartender": ("Bartender", "🍸", 0, 150, 340, "Know your cocktails"),
    "dealer": ("Card dealer", "🃏", 10, 220, 460, "Count the hand"),
    "bouncer": ("Bouncer", "🕶️", 25, 260, 540, "Spot the fake ID"),
    "dj": ("DJ", "🎧", 45, 320, 650, "Read the crowd"),
}
COCKTAILS = {
    "Mojito": ["white rum", "mint", "lime", "soda"], "Margarita": ["tequila", "triple sec", "lime", "salt"],
    "Old Fashioned": ["bourbon", "bitters", "sugar", "orange peel"],
    "Cosmopolitan": ["vodka", "cranberry", "triple sec", "lime"],
    "Piña Colada": ["rum", "coconut cream", "pineapple", "ice"], "Negroni": ["gin", "campari", "sweet vermouth"],
    "Espresso Martini": ["vodka", "espresso", "coffee liqueur"], "Moscow Mule": ["vodka", "ginger beer", "lime"],
    "Daiquiri": ["white rum", "lime", "sugar syrup"], "Aperol Spritz": ["aperol", "prosecco", "soda"],
}
CARD_VALUES = {"A": 11, "K": 10, "Q": 10, "J": 10, "10": 10, "9": 9, "8": 8, "7": 7, "6": 6, "5": 5, "4": 4,
               "3": 3, "2": 2}
SUITS = "♠♥♦♣"
FIRST_NAMES = ["Jade", "Marco", "Tia", "Dre", "Nina", "Leo", "Kai", "Rosa", "Vince", "Lou", "Mia", "Zeke"]
CROWDS = {"drill": (138, 145), "house": (122, 128), "hip-hop": (85, 98), "techno": (130, 136), "reggaeton": (88, 96),
          "drum & bass": (170, 176), "lo-fi": (70, 85)}


def blackjack_total(cards: list[str]) -> int:
    total = sum(CARD_VALUES[c] for c in cards)
    aces = cards.count("A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def make_task(job: str, rng: random.Random, year: int | None = None) -> tuple[str, list[str], int]:
    """(question, four answers, index of the right one) for a job's mini-game."""
    if job == "valet":
        letters = "ABCDEFGHJKLMNPRSTVWXYZ"
        plate = f"NV {rng.choice(letters)}{rng.randint(1, 9)}{rng.choice(letters)}-{rng.randint(10, 99)}"
        fakes = set()
        while len(fakes) < 3:
            chars = list(plate)
            spots = [i for i, ch in enumerate(chars) if ch.isalnum() and i > 2]
            i = rng.choice(spots)
            chars[i] = rng.choice(letters) if chars[i].isalpha() else str(rng.randint(0, 9))
            fake = "".join(chars)
            if fake != plate:
                fakes.add(fake)
        options = list(fakes) + [plate]
        rng.shuffle(options)
        return f"A guest hands you their ticket: **`{plate}`**. Which key?", options, options.index(plate)
    if job == "bartender":
        drink, parts = rng.choice(list(COCKTAILS.items()))
        right = rng.choice(parts)
        wrong_pool = sorted({p for d, ps in COCKTAILS.items() if d != drink for p in ps} - set(parts))
        options = rng.sample(wrong_pool, 3) + [right]
        rng.shuffle(options)
        return f"Someone orders a **{drink}**. Which of these goes in it?", options, options.index(right)
    if job == "dealer":
        hand = [rng.choice(list(CARD_VALUES)) for _ in range(rng.randint(2, 4))]
        total = blackjack_total(hand)
        shown = "  ".join(f"`{c}{rng.choice(SUITS)}`" for c in hand)
        wrong = set()
        while len(wrong) < 3:
            guess = total + rng.choice([-10, -3, -2, -1, 1, 2, 3, 10])
            if guess > 0 and guess != total:
                wrong.add(guess)
        options = [str(x) for x in list(wrong) + [total]]
        rng.shuffle(options)
        return f"The player's hand: {shown}\nWhat does it count as? (Aces are 11 or 1)", options, options.index(str(total))
    if job == "bouncer":
        year = year or datetime.now().year
        names = rng.sample(FIRST_NAMES, 4)
        fake = rng.randrange(4)
        options = []
        for i, name in enumerate(names):
            age = rng.randint(17, 20) if i == fake else rng.randint(21, 58)
            options.append(f"{name}, born {year - age}")
        return f"It's {year}. Entry is 21+. Who's **underage**?", options, fake
    # dj
    crowd, (low, high) = rng.choice(list(CROWDS.items()))
    right = rng.randint(low, high)
    others = [g for g in CROWDS if g != crowd]
    options = [f"{rng.randint(*CROWDS[g])} BPM" for g in rng.sample(others, 3)] + [f"{right} BPM"]
    rng.shuffle(options)
    return f"The crowd wants **{crowd}**. Which track do you drop next?", options, options.index(f"{right} BPM")


def shift_pay(low: int, high: int, correct: bool, seconds: float, rng: random.Random) -> tuple[int, str]:
    base = rng.randint(low, high)
    if not correct:
        return int(base * 0.25), "Wrong call. The manager docks most of your pay."
    if seconds <= 6:
        return int(base * 1.25), "Fast and flawless: a tip on top."
    if seconds <= 12:
        return base, "Solid shift."
    return int(base * 0.8), "Right, but slow. The customer tipped less."


class ShiftView(discord.ui.View):
    """The shift's four answers. Calls `finish(picked or None, seconds)` once: on a pick or when time runs out."""

    def __init__(self, owner: int, options: list[str], finish) -> None:
        super().__init__(timeout=JOB_SECONDS)
        self.owner = owner
        self.finish = finish
        self.picked: int | None = None
        self.started = time.monotonic()
        self.message: discord.Message | None = None
        self._over = False
        for i, label in enumerate(options):
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)
            button.callback = self._choose(i)
            self.add_item(button)

    def _choose(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner:
                await interaction.response.send_message("This isn't your shift.", ephemeral=True)
                return
            if self._over:
                await interaction.response.defer()
                return
            self._over = True
            self.picked = index
            took = time.monotonic() - self.started
            for child in self.children:
                child.disabled = True
            self.stop()
            card = await self.finish(index, took)
            await interaction.response.edit_message(embed=card, view=self)
        return callback

    async def on_timeout(self) -> None:
        if self._over:
            return
        self._over = True
        for child in self.children:
            child.disabled = True
        card = await self.finish(None, float(JOB_SECONDS))
        if self.message is not None:
            try:
                await self.message.edit(embed=card, view=self)
            except discord.HTTPException:
                pass


# Stocks

TICKERS = {
    # ticker: (company, icon, what moves it)
    "SYKE": ("SYKE Inc", "🟣", "how busy the chat is"),
    "LOL": ("LOL Corp", "😂", "laughs (lol, lmao, 😂, 💀)"),
    "VOX": ("Vox Media", "🎙️", "people in voice channels"),
    "CHIP": ("Chip & Co", "🎰", "casino action"),
    "NITE": ("Night Owl", "🦉", "late-night chatter"),
    "DRMA": ("Drama Ltd", "🍿", "deleted and edited messages"),
}
TICK_SECONDS = 15 * 60
HISTORY = 96                    # 24h of ticks
START_PRICE = 100.0
FEE = 0.01
MAX_SHARES = 100_000
LAUGHS = re.compile(r"\b(lol+|lmf?ao+|haha+|ha(ha)+|xd+|dead|crying)\b|😂|🤣|💀|😭", re.I)
SPARK = "▁▂▃▄▅▆▇█"
STOCKS_KEY = "stocks"
HOLDINGS_KEY = "stock_holdings"


def sparkline(values: list[float], width: int = 16) -> str:
    if not values:
        return ""
    values = values[-width:]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[3] * len(values)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


def next_price(price: float, signal: float, baseline: float, rng: random.Random) -> float:
    """Move a price by how far this tick's activity is above or below its usual level."""
    ratio = (signal + 1) / (baseline + 1)
    drift = max(-0.12, min(0.12, 0.08 * math.log(ratio)))
    pull = (START_PRICE - price) / START_PRICE * 0.01     # a gentle pull back towards 100
    noise = rng.gauss(0, 0.012)
    return round(max(1.0, price * (1 + drift + pull + noise)), 2)


def parse_shares(text: str, held: int, affordable: int) -> int | None:
    text = (text or "").strip().lower()
    if text in ("all", "max"):
        return held if held else affordable
    if text == "half":
        return (held or affordable) // 2
    if text.isdigit():
        return int(text)
    return None


class StripExtras(commands.Cog, name="StripExtras"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.rng = random.Random()
        self.counts: dict[int, dict[str, float]] = {}

    async def cog_load(self) -> None:
        self.market_loop.start()

    async def cog_unload(self) -> None:
        self.market_loop.cancel()

    @property
    def eco(self):
        return self.bot.get_cog("Economy")

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    # Weekly

    @commands.hybrid_command(name="weekly", aliases=["week"], with_app_command=False,
                             description="Claim your weekly cash (claim every week for more)")
    async def weekly(self, ctx: commands.Context) -> None:
        eco = self.eco
        w = await eco._guard(ctx) if eco else None
        if w is None:
            return
        now = time.time()
        if w.get("week_at") and now - w["week_at"] < WEEK_COOLDOWN:
            await ctx.reply(embed=embed("Already claimed this week", f"Come back <t:{int(w['week_at'] + WEEK_COOLDOWN)}:R>.",
                                        WARN), ephemeral=True)
            return
        streak = next_week_streak(w.get("week_at") or 0, now, int(w.get("week_streak") or 0))
        amount = int(weekly_reward(streak) * eco.happy(ctx.guild.id))
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=amount, week_at=now, week_streak=streak)
        body = f"You picked up {eco.money(amount)}."
        lost = int(w.get("week_streak") or 0) if streak == 1 and w.get("week_at") else 0
        if lost >= 2:
            body += (f"\n💔 **You lost your {lost}-week streak.** That's "
                     f"{eco.money(weekly_reward(lost + 1) - weekly_reward(1))} less than you'd have got. Back to week 1.")
        elif streak > 1:
            body += f"\n🔥 **{streak}-week streak.** Next week pays {eco.money(weekly_reward(streak + 1))}."
        body += f"\nNext weekly <t:{int(now + WEEK_COOLDOWN)}:R> · miss a week and the streak resets."
        await ctx.reply(embed=embed(f"{eco.e('bank')} Weekly collected", body, OK))

    # Jobs

    def job_of(self, guild_id: int, user_id: int) -> str:
        return (self.bot.storage.get_config(guild_id, "jobs").get(str(user_id))) or "valet"

    @commands.hybrid_command(name="jobs", aliases=["careers"], with_app_command=False,
                             description="See the Strip's jobs and switch to one you've unlocked")
    async def jobs(self, ctx: commands.Context, *, job: str = "") -> None:
        eco = self.eco
        w = await eco._guard(ctx) if eco else None
        if w is None:
            return
        shifts = int(w.get("job_shifts") or 0)
        current = self.job_of(ctx.guild.id, ctx.author.id)
        p = self.p(ctx)
        if job:
            key = job.strip().lower().replace("card ", "").replace(" ", "")
            key = {"card": "dealer", "carddealer": "dealer", "bar": "bartender", "door": "bouncer",
                   "security": "bouncer", "parking": "valet"}.get(key, key)
            if key not in JOBS:
                await ctx.reply(embed=embed("No such job", f"Pick one of: {', '.join(JOBS)}.", WARN), ephemeral=True)
                return
            title, icon, needs, *_ = JOBS[key]
            if shifts < needs:
                await ctx.reply(embed=embed("Not yet", f"{icon} **{title}** unlocks after **{needs}** shifts. "
                                                       f"You've worked {shifts}.", WARN), ephemeral=True)
                return
            self.bot.storage.update_config(ctx.guild.id, "jobs", **{str(ctx.author.id): key})
            await ctx.reply(embed=embed("New job 🪪", f"You're a **{title}** {icon} now. Clock in with `{p}job`.", OK))
            return
        lines = []
        for key, (title, icon, needs, low, high, what) in JOBS.items():
            state = "← your job" if key == current else ("🔒 " + f"{needs} shifts" if shifts < needs else "")
            lines.append(f"{icon} **{title}** · {eco.money(low)}–{high:,} · *{what}* {state}")
        card = embed("🪪 Jobs on the Strip", "\n".join(lines))
        card.set_footer(text=f"{shifts} shifts worked · switch with {p}jobs <name> · work with {p}job")
        await ctx.reply(embed=card)

    @commands.hybrid_command(name="job", aliases=["clockin", "jobshift"], with_app_command=False,
                             description="Work a shift at your job: a quick mini-game for cash")
    async def job(self, ctx: commands.Context) -> None:
        eco = self.eco
        w = await eco._guard(ctx) if eco else None
        if w is None or not await eco._free(ctx, w):
            return
        now = time.time()
        if w.get("job_at") and now - w["job_at"] < JOB_COOLDOWN:
            await ctx.reply(embed=embed("Off the clock", f"Your next shift starts <t:{int(w['job_at'] + JOB_COOLDOWN)}:R>.",
                                        WARN), ephemeral=True)
            return
        key = self.job_of(ctx.guild.id, ctx.author.id)
        title, icon, _, low, high, what = JOBS[key]
        question, options, right = make_task(key, self.rng)
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, job_at=now)  # the shift starts now
        shifts_before = int(w.get("job_shifts") or 0)
        guild_id, user_id, p = ctx.guild.id, ctx.author.id, self.p(ctx)

        async def finish(picked: int | None, took: float) -> discord.Embed:
            return self.settle_shift(guild_id, user_id, key, question, options, right, picked, took, shifts_before, p)

        view = ShiftView(ctx.author.id, options, finish)
        card = embed(f"{icon} {title} shift", f"{question}\n\n*{JOB_SECONDS} seconds.*")
        view.message = await ctx.reply(embed=card, view=view)

    def settle_shift(self, guild_id: int, user_id: int, key: str, question: str, options: list[str], right: int,
                     picked: int | None, took: float, shifts_before: int, p: str) -> discord.Embed:
        eco = self.eco
        title, icon, _, low, high, _ = JOBS[key]
        correct = picked == right
        if picked is None:
            pay, verdict = 0, "You froze. No pay this shift."
        else:
            pay, verdict = shift_pay(low, high, correct, took, self.rng)
        pay = int(pay * eco.happy(guild_id))
        shifts = shifts_before + 1
        self.bot.storage.econ_adjust(guild_id, user_id, cash=pay, job_shifts=shifts)
        result = embed(f"{icon} Shift done", f"{question}\n\n{'✅' if correct else '❌'} The answer was "
                                             f"**{options[right]}**.\n{verdict} You earned {eco.money(pay)}.",
                       OK if correct else WARN)
        unlocked = [f"{JOBS[k][1]} {JOBS[k][0]}" for k in JOBS if JOBS[k][2] == shifts]
        if unlocked:
            result.add_field(name="Unlocked", value=f"{', '.join(unlocked)}: switch with `{p}jobs <name>`")
        result.set_footer(text=f"{shifts} shifts worked · next shift in {JOB_COOLDOWN // 60} min")
        return result

    # Stocks: activity counters

    def _count(self, guild_id: int, key: str, amount: float = 1.0) -> None:
        bucket = self.counts.setdefault(guild_id, {})
        bucket[key] = bucket.get(key, 0.0) + amount

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        gid = message.guild.id
        self._count(gid, "SYKE")
        if LAUGHS.search(message.content or ""):
            self._count(gid, "LOL")
        hour = datetime.now(self.bot.tz_for(gid)).hour
        if hour < 5:
            self._count(gid, "NITE")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        if payload.guild_id and str(payload.emoji) in ("😂", "🤣", "💀", "😭"):
            self._count(payload.guild_id, "LOL", 0.5)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload) -> None:
        if payload.guild_id:
            self._count(payload.guild_id, "DRMA")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload) -> None:
        if getattr(payload, "guild_id", None):
            self._count(payload.guild_id, "DRMA", 0.5)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        cog = getattr(ctx.command, "cog_name", "") or ""
        if ctx.guild and cog in ("Economy", "Casino", "CardGames"):
            self._count(ctx.guild.id, "CHIP")

    # Stocks: the market

    def market(self, guild_id: int) -> dict:
        data = self.bot.storage.get_config(guild_id, STOCKS_KEY)
        prices = data.get("prices") or {}
        for t in TICKERS:
            prices.setdefault(t, START_PRICE)
        data["prices"] = prices
        data.setdefault("base", {})
        data.setdefault("history", {})
        return data

    def tick(self, guild, now: float | None = None) -> dict:
        """Move every price once, from the activity counted since the last tick."""
        data = self.market(guild.id)
        counts = self.counts.pop(guild.id, {})
        counts["VOX"] = sum(sum(1 for m in getattr(vc, "members", []) if not m.bot)
                            for vc in getattr(guild, "voice_channels", []))
        for t in TICKERS:
            signal = float(counts.get(t, 0.0))
            base = float(data["base"].get(t, signal))
            data["prices"][t] = next_price(float(data["prices"][t]), signal, base, self.rng)
            data["base"][t] = round(base * 0.9 + signal * 0.1, 3)         # the usual level, slowly learned
            hist = list(data["history"].get(t) or [])
            hist.append(data["prices"][t])
            data["history"][t] = hist[-HISTORY:]
        data["updated"] = now or time.time()
        self.bot.storage.set_config(guild.id, STOCKS_KEY, data)
        return data

    @tasks.loop(seconds=TICK_SECONDS)
    async def market_loop(self) -> None:
        eco = self.eco
        for guild in list(self.bot.guilds):
            if eco is not None and eco._open(guild.id):
                self.tick(guild)

    @market_loop.before_loop
    async def _before_market(self) -> None:
        await self.bot.wait_until_ready()

    def holdings(self, guild_id: int, user_id: int) -> dict:
        return dict((self.bot.storage.get_config(guild_id, HOLDINGS_KEY).get(str(user_id))) or {})

    def save_holdings(self, guild_id: int, user_id: int, held: dict) -> None:
        all_held = dict(self.bot.storage.get_config(guild_id, HOLDINGS_KEY))
        held = {t: v for t, v in held.items() if v and v[0] > 0}
        if held:
            all_held[str(user_id)] = held
        else:
            all_held.pop(str(user_id), None)
        self.bot.storage.set_config(guild_id, HOLDINGS_KEY, all_held)

    def market_card(self, guild_id: int) -> discord.Embed:
        data = self.market(guild_id)
        lines = []
        for t, (company, icon, driver) in TICKERS.items():
            price = data["prices"][t]
            hist = data["history"].get(t) or [price]
            first = hist[0] if hist else price
            change = (price - first) / first * 100 if first else 0.0
            arrow = "🟢" if change > 0.05 else ("🔴" if change < -0.05 else "⚪")
            lines.append(f"{icon} **{t}** · {company}\n`{sparkline(hist)}` **{price:,.2f}** "
                         f"{arrow} {change:+.1f}% · *moves with {driver}*")
        card = embed("📈 Strip Stock Exchange", "\n".join(lines), BRAND)
        updated = data.get("updated")
        card.set_footer(text="Prices move every 15 minutes with what happens in this server · last 24h shown"
                        + ("" if updated else " · market opens on the next tick"))
        return card

    @commands.group(name="stocks", aliases=["stock", "sx", "stonks"], case_insensitive=True,
                    invoke_without_command=True,
                    description="The Strip Stock Exchange: prices move with what happens in the server")
    async def stocks(self, ctx: commands.Context) -> None:
        eco = self.eco
        if eco is None or await eco._guard(ctx) is None:
            return
        card = self.market_card(ctx.guild.id)
        p = self.p(ctx)
        card.description += (f"\n\n`{p}stocks buy SYKE 10` · `{p}stocks sell SYKE all` · `{p}stocks portfolio`")
        await ctx.reply(embed=card)

    @stocks.command(name="buy", aliases=["b"])
    async def stocks_buy(self, ctx: commands.Context, ticker: str, amount: str = "1") -> None:
        await self.trade(ctx, ticker, amount, buying=True)

    @stocks.command(name="sell", aliases=["s"])
    async def stocks_sell(self, ctx: commands.Context, ticker: str, amount: str = "all") -> None:
        await self.trade(ctx, ticker, amount, buying=False)

    async def trade(self, ctx: commands.Context, ticker: str, amount: str, *, buying: bool) -> None:
        eco = self.eco
        w = await eco._guard(ctx) if eco else None
        if w is None or not await eco._free(ctx, w):
            return
        t = ticker.upper().lstrip("$")
        if t not in TICKERS:
            await ctx.reply(embed=embed("Unknown stock", f"Tickers: {', '.join(TICKERS)}.", WARN), ephemeral=True)
            return
        price = float(self.market(ctx.guild.id)["prices"][t])
        held = self.holdings(ctx.guild.id, ctx.author.id)
        shares, cost = held.get(t, [0, 0.0])
        if buying:
            affordable = int(w["cash"] / (price * (1 + FEE)))
            qty = parse_shares(amount, 0, affordable)
            if not qty or qty <= 0:
                await ctx.reply(embed=embed("How many?", f"You can afford **{affordable:,}** {t} right now.", WARN),
                                ephemeral=True)
                return
            if shares + qty > MAX_SHARES:
                await ctx.reply(embed=embed("Too many", f"You can hold up to {MAX_SHARES:,} shares of {t}.", WARN),
                                ephemeral=True)
                return
            total = math.ceil(qty * price * (1 + FEE))
            if not self.bot.storage.econ_spend(ctx.guild.id, ctx.author.id, total):
                await ctx.reply(embed=embed("Not enough cash", f"{qty:,} {t} costs {eco.money(total)} with the fee.",
                                            WARN), ephemeral=True)
                return
            held[t] = [shares + qty, round(cost + total, 2)]
            self.save_holdings(ctx.guild.id, ctx.author.id, held)
            await ctx.reply(embed=embed("📈 Bought", f"**{qty:,} {t}** at {price:,.2f} for {eco.money(total)} "
                                                    f"(1% fee). You hold {shares + qty:,}.", OK))
            return
        qty = parse_shares(amount, shares, 0)
        if not qty or qty <= 0 or qty > shares:
            await ctx.reply(embed=embed("How many?", f"You hold **{shares:,}** {t}.", WARN), ephemeral=True)
            return
        gross = int(qty * price)
        fee = math.ceil(gross * FEE)
        paid_for = cost * qty / shares if shares else 0
        self.bot.storage.econ_adjust(ctx.guild.id, ctx.author.id, cash=gross - fee)
        held[t] = [shares - qty, round(cost - paid_for, 2)]
        self.save_holdings(ctx.guild.id, ctx.author.id, held)
        profit = gross - fee - paid_for
        await ctx.reply(embed=embed("📉 Sold", f"**{qty:,} {t}** at {price:,.2f} for {eco.money(gross - fee)}.\n"
                                              f"{'Profit' if profit >= 0 else 'Loss'}: {eco.money(abs(int(profit)))}",
                                    OK if profit >= 0 else WARN))

    @stocks.command(name="portfolio", aliases=["p", "mine", "holdings"])
    async def stocks_portfolio(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        eco = self.eco
        if eco is None or await eco._guard(ctx) is None:
            return
        who = member or ctx.author
        held = self.holdings(ctx.guild.id, who.id)
        prices = self.market(ctx.guild.id)["prices"]
        if not held:
            await ctx.reply(embed=embed(f"📊 {who.display_name}'s portfolio", "No shares yet."))
            return
        lines, value, paid = [], 0.0, 0.0
        for t, (shares, cost) in held.items():
            now_value = shares * float(prices.get(t, START_PRICE))
            value += now_value
            paid += cost
            diff = now_value - cost
            lines.append(f"{TICKERS[t][1]} **{t}** · {shares:,} shares · worth {eco.money(int(now_value))} "
                         f"({'+' if diff >= 0 else '-'}{abs(int(diff)):,})")
        total = value - paid
        card = embed(f"📊 {who.display_name}'s portfolio", "\n".join(lines), OK if total >= 0 else WARN)
        card.set_footer(text=f"Worth {int(value):,} · paid {int(paid):,} · {'up' if total >= 0 else 'down'} "
                             f"{abs(int(total)):,}")
        await ctx.reply(embed=card)
