"""Life on THE STRIP: storefront shops & items, crime & jail, and owning the Strip itself.

Builds on the Economy cog (wallets, the House, jail checks) and the art module
(the neon Strip map). Like economy.py, all the money math lives in plain
functions and catalogs up top so it's unit-testable; the `StripWorld` cog is a
thin Discord layer.

* **Shops** — every storefront sells something: scratchers and padlocks at the
  General Store, food buffs at the Golden Dragon, luck from Madame Rose, a custom
  name colour from Sherwin Williams, rare vinyl from Dead Air Records… `pawn`
  sells things back at Pawn Deals.
* **Crime** — `rob` someone's cash on hand (the bank is safe, padlocks stop it),
  pull a solo `crime`, or `heist` a storefront's register. Get caught and you're
  fined and jailed: no earning or gambling until you're out or someone `bail`s you.
* **Property** — 11 unique storefronts per server. Buy one, upgrade it, and it
  earns every hour into a register (which holds up to a day). Owners also take a
  cut of everything sold in their store, so collect before someone heists it.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import re
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from . import casino_art as art
from .moderation import BRAND, OK, WARN, embed

HOUR = 3600


# ── storefronts ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Store:
    key: str
    name: str
    colour: tuple[int, int, int]
    price: int
    income: int          # cash per hour at level 1
    blurb: str


STORES = [
    Store("video", "Video 2000", (62, 230, 255), 8_000, 60, "Rentals, late fees and a back room."),
    Store("photo", "1-Hour Photo", (255, 214, 92), 9_000, 65, "Develops film. Sees everything."),
    Store("nails", "Nails 4 U", (255, 62, 165), 12_000, 85, "Acrylics, gossip and tips."),
    Store("records", "Dead Air Records", (176, 120, 255), 14_000, 100, "Crates of vinyl, some of it rare."),
    Store("general", "General Store", (255, 108, 64), 18_000, 130, "Scratchers, snacks and padlocks."),
    Store("rose", "Madame Rose", (255, 96, 150), 20_000, 140, "Palms read. Fortunes told. No refunds."),
    Store("paint", "Sherwin Williams", (80, 168, 255), 25_000, 175, "Custom colours, by the gallon."),
    Store("cash4less", "Cash 4 Less", (88, 230, 120), 30_000, 210, "Cheques cashed, questions not asked."),
    Store("pawn", "Pawn Deals", (255, 172, 40), 35_000, 240, "Buys anything. Pays almost nothing."),
    Store("dragon", "Golden Dragon", (255, 66, 66), 45_000, 300, "The best lo mein on the Strip."),
    Store("bail", "Bail Bonds", (255, 222, 64), 60_000, 400, "Gets you out. For a fee."),
]
# THE STRIP 3.21: more of the game's storefronts can be owned (from the game's phone; the Discord property map keeps the
# original eleven)
GAME_STORES = [
    Store("laundro", "Spin Cycle", (120, 200, 255), 9_500, 68, "Coin laundry. One machine eats quarters on purpose."),
    Store("quikstop", "Quik Stop", (255, 150, 60), 10_000, 72, "Corner store. Lottery, smokes, and a cat on the counter."),
    Store("thrift", "Second Hand", (210, 170, 120), 11_000, 80, "Vintage, used, and some of it still warm."),
    Store("books", "Fifth Avenue Books", (170, 130, 90), 15_000, 108, "New, used and badly shelved."),
    Store("liquor", "Liquor Barn", (200, 80, 80), 16_000, 115, "Open 24 hours. Judges you 24 hours."),
    Store("tattoo", "Ink & Regret", (150, 90, 200), 19_000, 135, "Walk-ins welcome. Spell-check not included."),
    Store("gym", "Iron Lung Gym", (230, 90, 70), 21_000, 150, "Smells like effort."),
    Store("shades", "Specs", (90, 200, 220), 22_000, 155, "Shades, tints and frames."),
    Store("carwash", "Suds Car Wash", (80, 170, 255), 22_000, 155, "Hand wash, wax, and no questions about the stains."),
    Store("hats", "Mad Hatter", (230, 120, 60), 24_000, 170, "Berets, top hats, trappers and caps."),
    Store("masks", "Face Value", (120, 220, 140), 24_000, 170, "Masks and costumes."),
    Store("cafe", "Blossom Café", (255, 170, 200), 26_000, 180, "Flat whites and warm croissants."),
    Store("lastcall", "Last Call", (200, 60, 90), 27_000, 190, "Dive bar. The jukebox only plays sad songs."),
    Store("threads", "Threads", (240, 70, 140), 28_000, 195, "Jackets, shirts, trousers and everything in between."),
    Store("diner", "Rattlesnake Diner", (240, 200, 80), 32_000, 220, "Pie, coffee and truckers."),
    Store("karaoke", "Sing Sing", (176, 120, 255), 38_000, 260, "Karaoke till 4am."),
    Store("gasmart", "Gas-N-Go", (255, 200, 60), 40_000, 275, "Gas, jerky, and the cleanest bathroom in the desert."),
    Store("jewels", "Glitter Jewelers", (255, 220, 120), 50_000, 330, "Gold, silver, pearls and ice, behind glass."),
    Store("bigbun", "Big Bun Burgers", (48, 200, 190), 30_000, 210, "Burgers, fries and a drive-up window."),
    Store("cluck", "Cluck Shack", (240, 140, 50), 28_000, 200, "Fried chicken by the bucket."),
    Store("pizzarocket", "Pizza Rocket", (150, 90, 220), 30_000, 210, "Pizza, launched to your door."),
    Store("tacocomet", "Taco Comet", (140, 220, 70), 26_000, 185, "Tacos, nachos and slushies at the speed of light."),
]
ALL_STORES = STORES + GAME_STORES
STORE_BY_KEY = {s.key: s for s in ALL_STORES}

MAX_LEVEL = 5
REGISTER_CAP_HOURS = 24        # a register holds a day's takings; collect or it stops filling
SELL_BACK = 0.6                # selling a storefront returns 60% of everything put into it
OWNER_CUT = 0.10               # store owners get 10% of what's sold in their store
BAIL_CUT = 0.25                # the Bail Bonds owner keeps 25% of bail paid


INCOME_MULT = 2.5   # 2026-11: storefronts were a poor investment (a Video 2000 took 5 days to pay back); now about two


def shop_income(store: Store, level: int) -> int:
    """Cash per hour at a given level (+50% per level above 1)."""
    return int(store.income * INCOME_MULT * (1 + 0.5 * (max(1, level) - 1)))


def upgrade_cost(store: Store, level: int) -> int:
    """What it costs to take a storefront from `level` to `level + 1`."""
    return int(store.price * 0.6 * level)


MARKUP_RANGE = (-0.25, 0.5)    # owners set their prices: 25% off up to 50% over


def demand(markup: float) -> float:
    """Walk-in trade at a price level: a sale brings more people in, a markup sends some away."""
    m = max(MARKUP_RANGE[0], min(MARKUP_RANGE[1], float(markup or 0)))
    return 1 - 0.8 * m


def pending(row: dict, store: Store, now: float) -> int:
    """Cash waiting in a storefront's register: tips plus hourly income (capped at a day), scaled by its prices."""
    hours = max(0.0, min(REGISTER_CAP_HOURS, (now - row["collected_at"]) / HOUR))
    return int(row["register"] + shop_income(store, row["level"]) * demand(row.get("markup", 0)) * hours)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _best(query: str, entries):
    """Match `query` against (key, name, obj) entries: exact, then prefix, then a word, then anywhere."""
    q = _norm(query)
    if not q:
        return None
    entries = list(entries)
    tests = (
        lambda k, n: q in (_norm(k), _norm(n)),
        lambda k, n: _norm(k).startswith(q) or _norm(n).startswith(q),
        lambda k, n: any(w.startswith(q) for w in re.findall(r"[a-z0-9]+", n.lower())),
        lambda k, n: q in _norm(n),
    )
    for test in tests:
        for key, name, obj in entries:
            if test(key, name):
                return obj
    return None


def find_store(text: str) -> Store | None:
    return _best(text, ((s.key, s.name, s) for s in STORES))


# ── items ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Item:
    key: str
    name: str
    emoji: str
    store: str
    price: int
    blurb: str
    buff: str | None = None
    hours: float = 0
    passive: bool = False      # works automatically from your inventory


ITEMS = [
    Item("scratch", "Scratch Ticket", "🎟️", "general", 100, "Scratch for a shot at 5,000."),
    Item("energy", "Energy Drink", "🥤", "general", 250, "Skip your work cooldown."),
    Item("padlock", "Padlock", "🔒", "general", 800, "Stops the next robbery on you (automatic).", passive=True),
    Item("bag", "Mystery Bag", "🛍️", "general", 400, "Could be anything on the Strip."),
    Item("cookie", "Fortune Cookie", "🥠", "dragon", 150, "A fortune and an hour of luck.", "lucky", 1),
    Item("lomein", "Lo Mein Combo", "🍜", "dragon", 300, "+25% work pay for 2 hours.", "fed", 2),
    Item("latte", "Flat White", "☕", "cafe", 120, "Halves your hustle cooldown for 1 hour.", "chill", 1),
    Item("croissant", "Warm Croissant", "🥐", "cafe", 90, "Buttery. Fills you up a little."),
    Item("novel", "Paperback Novel", "📖", "books", 200, "An hour of luck. It is a good book.", "lucky", 1),
    Item("set", "Fresh Set", "💅", "nails", 500, "+15% work pay for 3 hours (better tips).", "glam", 3),
    Item("tarot", "Tarot Reading", "🔮", "rose", 600, "For an hour, crime can't land you in jail.", "foresight", 1),
    Item("charm", "Lucky Charm", "🍀", "rose", 1_500, "6 hours of better odds on hustles and crime.", "lucky", 6),
    Item("rental", "Movie Rental", "📼", "video", 120, "Halves your hustle cooldown for 2 hours.", "chill", 2),
    Item("crate", "Vinyl Crate", "📦", "records", 1_200, "Dig for a rare record — keep it or pawn it."),
    Item("jailcard", "Get-Out-of-Jail Card", "🗝️", "bail", 2_500, "Walk out of jail free (use it inside)."),
    Item("paintjob", "Paint Job", "🎨", "paint", 20_000, "Your own name colour: `use paintjob #ff3ea5`."),
    # THE STRIP 3.22: the fast-food joints (game storefronts, ownable from the game's phone)
    Item("wings", "Bucket of Wings", "🍗", "cluck", 160, "Crispy, saucy, a bit too many. +25% work pay for 1 hour.", "fed", 1),
    Item("tenders", "Tender Box", "🍗", "cluck", 80, "Three tenders and a dip. Fills you up."),
    Item("bigdouble", "Big Bun Double", "🍔", "bigbun", 140, "Two patties, too much cheese. +25% work pay for 1 hour.", "fed", 1),
    Item("fries", "Basket of Fries", "🍟", "bigbun", 55, "Salty, hot, gone in a minute."),
    Item("pizza", "Rocket Pizza", "🍕", "pizzarocket", 210, "A whole pie, delivered hot. +25% work pay for 2 hours.", "fed", 2),
    Item("knots", "Garlic Knots", "🥖", "pizzarocket", 70, "Buttery, garlicky, dangerous."),
    Item("tacos", "Taco Trio", "🌮", "tacocomet", 110, "Three tacos, one hot sauce. +25% work pay for 1 hour.", "fed", 1),
    Item("nachos", "Comet Nachos", "🧀", "tacocomet", 85, "A mountain of chips and cheese."),
    Item("slushie", "Comet Slushie", "🥤", "tacocomet", 50, "Brain freeze in three colours. Halves your hustle cooldown for 1 hour.", "chill", 1),
]
ITEM_BY_KEY = {i.key: i for i in ITEMS}
# Shops that exist in THE STRIP (the game) but are not ownable storefronts on the property map.
MIDTOWN_SHOPS = {"cafe": "Blossom Café", "books": "Fifth Avenue Books", "cluck": "Cluck Shack", "bigbun": "Big Bun Burgers", "pizzarocket": "Pizza Rocket", "tacocomet": "Taco Comet"}

BUFF_INFO = {  # buff -> (emoji, label)
    "lucky": ("🍀", "Lucky — better odds on hustles, crimes and robberies"),
    "fed": ("🍜", "Well fed — +25% work pay"),
    "glam": ("💅", "Glam — +15% work pay"),
    "foresight": ("🔮", "Foresight — crime can't land you in jail"),
    "chill": ("📼", "Chill — half the hustle cooldown"),
}
WORK_BUFFS = {"fed": 1.25, "glam": 1.15}
LUCK_BONUS = 0.10

# Collectible records from Vinyl Crates: (key, title, rarity, value). Pawned at 80% of value.
RECORDS = [
    ("rec_static", "Neon Coyotes — Desert Static", "common", 400),
    ("rec_tapes", "The Midnight Tapes — Side B", "common", 400),
    ("rec_glass", "Velvet Arcade — Glass Hearts", "common", 450),
    ("rec_casino", "Lucky Seven Orchestra — Casino Lights", "uncommon", 900),
    ("rec_dragon", "Strip Kids — Live at the Golden Dragon", "uncommon", 950),
    ("rec_omens", "Madame Rose & the Omens — Crystal Ball", "rare", 2_600),
    ("rec_first", "Dead Air — First Pressing", "epic", 7_500),
    ("rec_gold", "Gold Record: Jackpot Nights", "legendary", 30_000),
]
RECORD_BY_KEY = {r[0]: r for r in RECORDS}
RARITY_WEIGHT = {"common": 60, "uncommon": 25, "rare": 10, "epic": 4, "legendary": 1}
RARITY_EMOJI = {"common": "⚪", "uncommon": "🟢", "rare": "🔵", "epic": "🟣", "legendary": "🟡"}
RECORD_PAWN = 0.8
ITEM_PAWN = 0.4

SCRATCH_TABLE = [(0.005, 5_000), (0.03, 1_000), (0.08, 250), (0.15, 100)]   # EV 90 on a 100 ticket
BAG_TABLE = [("scratch", 22), ("energy", 18), ("cookie", 15), ("rental", 12), ("lomein", 10), ("set", 8),
             ("padlock", 6), ("tarot", 4), ("crate", 3), ("charm", 1.5), ("jailcard", 0.5)]

FORTUNES = [
    "The house always wins. Today, you are the house.",
    "A stranger at the Golden Dragon owes you money. Collect.",
    "Avoid Video 2000 after midnight.",
    "Your lucky number is 17. Black. Probably.",
    "Fortune favours the bold, and the people who deposit first.",
    "Someone is about to try your pockets. Bank it.",
    "The next card is higher. Or lower. Trust yourself.",
    "A neon sign will flicker for you tonight.",
]


def find_item(text: str) -> Item | None:
    return _best(text, ((i.key, i.name, i) for i in ITEMS))


def split_tail(text: str, pattern: str) -> tuple[str, str | None]:
    """Peel a trailing token (a quantity, 'all', a colour…) off free text: 'lo mein 2' -> ('lo mein', '2')."""
    m = re.match(rf"^(.*\S)\s+({pattern})$", (text or "").strip(), re.IGNORECASE)
    return (m.group(1), m.group(2)) if m else ((text or "").strip(), None)


def find_owned(text: str, held: dict[str, int]) -> str | None:
    """Resolve something the player holds — an item or a record — to its inventory key."""
    item = find_item(text)
    if item and held.get(item.key):
        return item.key
    owned = [(k, RECORD_BY_KEY[k][1], k) for k in held if k in RECORD_BY_KEY]
    rec = _best(text, owned)
    if rec:
        return rec
    return item.key if item else None


def label(key: str) -> str:
    if key in ITEM_BY_KEY:
        i = ITEM_BY_KEY[key]
        return f"{i.emoji} {i.name}"
    if key in RECORD_BY_KEY:
        _, title, rarity, _ = RECORD_BY_KEY[key]
        return f"{RARITY_EMOJI[rarity]} {title}"
    from . import strip_kitchen   # ingredients and dishes from THE STRIP's kitchens
    return strip_kitchen.label(key)


def pawn_value(key: str) -> int:
    if key in RECORD_BY_KEY:
        return int(RECORD_BY_KEY[key][3] * RECORD_PAWN)
    if key in ITEM_BY_KEY:
        return int(ITEM_BY_KEY[key].price * ITEM_PAWN)
    from . import strip_kitchen
    return strip_kitchen.value(key)


def scratch(rng: random.Random) -> int:
    roll = rng.random()
    edge = 0.0
    for chance, prize in SCRATCH_TABLE:
        edge += chance
        if roll < edge:
            return prize
    return 0


def open_crate(rng: random.Random) -> str:
    tier = rng.choices(list(RARITY_WEIGHT), weights=list(RARITY_WEIGHT.values()))[0]
    return rng.choice([r[0] for r in RECORDS if r[2] == tier])


def open_bag(rng: random.Random) -> str:
    keys, weights = zip(*BAG_TABLE)
    return rng.choices(keys, weights=weights)[0]


HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex(text: str) -> int | None:
    m = HEX.match((text or "").strip())
    return int(m.group(1), 16) if m else None


# ── crime & jail ───────────────────────────────────────────────────────────
ROB_COOLDOWN = 2 * HOUR
CRIME_COOLDOWN = HOUR
HEIST_COOLDOWN = 3 * HOUR
ROB_MIN_CASH = 250             # both robber and target need this much on hand
ROB_CHANCE = 0.45
CRIME_CHANCE = 0.45
HEIST_CHANCE = 0.35
HEIST_MIN = 200                # a register needs at least this much to be worth hitting
ROB_JAIL_MIN = 30
HEIST_JAIL_MIN = 45

CRIMES = [
    "rolled a tourist's rental car for parts behind **Cash 4 Less**",
    "ran a rigged three-card monte outside the **Golden Dragon**",
    "sold 'genuine' Rolexes out of a trench coat by **Video 2000**",
    "skimmed the change machines at the laundromat",
    "talked your way into the casino's comp closet",
    "fenced a crate of 'fell off a truck' perfume through **Pawn Deals**",
]
CAUGHT = [
    "Strip security tackled you mid-sprint",
    "a dye pack went off in your bag",
    "your getaway driver went to the Golden Dragon instead",
    "1-Hour Photo had you on camera. In focus, for once",
]


def odds(base: float, lucky: bool) -> float:
    return min(0.95, base + (LUCK_BONUS if lucky else 0.0))


def rob_take(rng: random.Random, target_cash: int) -> int:
    return min(25_000, int(target_cash * rng.uniform(0.10, 0.30)))


def rob_fine(robber_cash: int) -> int:
    return max(150, int(robber_cash * 0.15))


def crime_payout(rng: random.Random) -> int:
    return rng.randint(400, 1_200)


def crime_fine(rng: random.Random) -> int:
    return rng.randint(200, 500)


def crime_jail_minutes(rng: random.Random) -> int:
    return rng.randint(20, 45)


def bail_cost(seconds_left: float) -> int:
    """Bail: a flat bond plus a fee for every minute still on your sentence."""
    return 250 + 25 * math.ceil(max(0.0, seconds_left) / 60)


# ── the cog ────────────────────────────────────────────────────────────────
class StripWorld(commands.Cog, name="StripWorld"):
    """Shops, crime, jail and storefront ownership on THE STRIP."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.rng = random.Random()

    @property
    def eco(self):
        return self.bot.get_cog("Economy")

    @property
    def db(self):
        return self.bot.storage

    def p(self, ctx) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    async def no(self, ctx, title: str, body: str) -> None:
        await ctx.reply(embed=embed(title, body, WARN), ephemeral=True)

    def cash(self, n: int) -> str:
        return self.eco.money(n)

    def buffs(self, gid: int, uid: int) -> dict[str, float]:
        return self.db.econ_buffs(gid, uid, time.time())

    def tip_store(self, gid: int, store_key: str, amount: int, buyer: int) -> None:
        row = self.db.econ_shop(gid, store_key)
        if row and amount > 0:
            self.db.econ_shop_tip(gid, store_key, amount)

    def jail(self, gid: int, uid: int, minutes: float) -> float:
        until = time.time() + minutes * 60
        self.db.econ_adjust(gid, uid, jail_until=until)
        return until

    # ── shops ──
    @commands.hybrid_command(name="shop", with_app_command=False, aliases=["store", "market"], description="Browse the Strip's storefronts")
    @app_commands.describe(store="One storefront to look inside (optional)")
    async def shop(self, ctx: commands.Context, *, store: str | None = None) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        owned = self.db.econ_shops(ctx.guild.id)
        p = self.p(ctx)
        love = self.bot.get_cog("Love")   # the chapel's rings and gifts (love.py), when SYKE is linked to the game
        if store and love is not None and love.is_chapel(store):
            await love.chapel.catalog()
            await ctx.reply(embed=love.chapel_embed(p))
            return
        if store:
            s = find_store(store)
            if s is None:
                await self.no(ctx, "Which store?", f"Try `{p}shop` to see them all.")
                return
            items = [i for i in ITEMS if i.store == s.key]
            lines = [f"{i.emoji} **{i.name}** · {self.cash(i.price)} · `{i.key}`\n*{i.blurb}*" for i in items]
            owner = owned.get(s.key)
            who = f"Owned by <@{owner['owner_id']}>" if owner else f"For sale · {self.cash(s.price)}"
            body = (f"*{s.blurb}*\n\n" + ("\n".join(lines) if lines else "Nothing for sale here — but you can own it.")
                    + f"\n\n{who}")
            e = embed(f"🏪 {s.name}", body, BRAND)
            e.set_footer(text=f"{p}buy <item>  •  {p}property buy {s.key}")
            await ctx.reply(embed=e)
            return
        e = embed("🏪 Shopping on the Strip", f"`{p}buy <item>` · `{p}use <item>` · `{p}pawn <item>` · "
                  f"`{p}shop <store>` for details", BRAND)
        for s in STORES:
            items = [i for i in ITEMS if i.store == s.key]
            if not items:
                continue
            e.add_field(name=s.name, value="\n".join(f"{i.emoji} `{i.key}` {i.price:,}" for i in items), inline=True)
        if love is not None:
            love.shop_field(e, p)
        e.set_footer(text="Prices in cash • store owners get 10% of every sale")
        await ctx.reply(embed=e)

    @commands.hybrid_command(name="buy", with_app_command=False, description="Buy an item from a Strip storefront")
    @app_commands.describe(item="What to buy, optionally followed by how many (e.g. 'scratch 5')")
    async def buy(self, ctx: commands.Context, *, item: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        item, n = split_tail(item, r"\d+")
        qty = int(n) if n else 1
        it = find_item(item)
        if it is None:
            await self.no(ctx, "Never heard of it", f"See what's for sale with `{self.p(ctx)}shop`.")
            return
        qty = max(1, min(qty, 50))
        total = it.price * qty
        if not self.db.econ_spend(ctx.guild.id, ctx.author.id, total):
            await self.no(ctx, "Not enough cash", f"{qty}× {it.name} costs {self.cash(total)} — you have "
                          f"{self.cash(w['cash'])} on hand.")
            return
        self.db.econ_item_add(ctx.guild.id, ctx.author.id, it.key, qty)
        self.tip_store(ctx.guild.id, it.store, int(total * OWNER_CUT), ctx.author.id)
        store = STORE_BY_KEY.get(it.store) or Store(it.store, MIDTOWN_SHOPS.get(it.store, it.store.title()), (255, 255, 255), 0, 0, "")
        how = "It works automatically." if it.passive else f"Use it with `{self.p(ctx)}use {it.key}`."
        await ctx.reply(embed=embed(f"{it.emoji} Bought", f"{qty}× **{it.name}** from {store.name} for "
                                    f"{self.cash(total)}. {how}", OK))

    @commands.hybrid_command(name="inventory", with_app_command=False, aliases=["inv", "items", "bag"], description="Your items and active buffs")
    @app_commands.describe(member="Whose inventory (defaults to you)")
    async def inventory(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        who = member or ctx.author
        held = self.db.econ_items(ctx.guild.id, who.id)
        buffs = self.buffs(ctx.guild.id, who.id)
        lines = [f"{label(k)} ×{n}" for k, n in held.items()]
        worth = sum(pawn_value(k) * n for k, n in held.items())
        body = "\n".join(lines) if lines else "Empty pockets."
        if buffs:
            body += "\n\n**Active**\n" + "\n".join(
                f"{BUFF_INFO[b][0]} {BUFF_INFO[b][1]} · ends <t:{int(t)}:R>" for b, t in buffs.items() if b in BUFF_INFO)
        e = embed(f"🎒 {who.display_name}'s inventory", body, BRAND)
        if worth:
            e.set_footer(text=f"Pawn value ≈ {worth:,} cash")
        await ctx.reply(embed=e)

    @commands.hybrid_command(name="use", with_app_command=False, aliases=["open", "eat"], description="Use an item from your inventory")
    @app_commands.describe(item="What to use (add a colour for a paint job, e.g. 'paintjob #ff3ea5')")
    async def use(self, ctx: commands.Context, *, item: str, arg: str | None = None) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        if arg is None:
            item, arg = split_tail(item, r"#?[0-9a-f]{6}")
        gid, uid = ctx.guild.id, ctx.author.id
        held = self.db.econ_items(gid, uid)
        key = find_owned(item, held)
        if key is None or not held.get(key):
            await self.no(ctx, "You don't have that", f"Check `{self.p(ctx)}inventory`, or buy one in the `shop`.")
            return
        if key in RECORD_BY_KEY:
            await self.no(ctx, "It's a record", "Spin it at home — or `pawn` it for cash.")
            return
        it = ITEM_BY_KEY[key]
        if it.passive:
            await self.no(ctx, "No need", f"Your {it.name} works automatically.")
            return
        now = time.time()
        if key == "paintjob":
            await self._paint(ctx, arg)
            return
        if key == "jailcard":
            if w["jail_until"] <= now:
                await self.no(ctx, "You're not locked up", "Save it for when you need it.")
                return
            self.db.econ_item_add(gid, uid, key, -1)
            self.db.econ_adjust(gid, uid, jail_until=0)
            await ctx.reply(embed=embed("🗝️ Free to go", "You flash the card. The guard sighs. You walk.", OK))
            return
        self.db.econ_item_add(gid, uid, key, -1)
        if key == "scratch":
            prize = scratch(self.rng)
            if prize:
                self.db.econ_adjust(gid, uid, cash=prize)
                await ctx.reply(embed=embed("🎟️ Winner!", f"You scratched off {self.cash(prize)}!", OK))
            else:
                await ctx.reply(embed=embed("🎟️ No luck", "Not a winner. The General Store thanks you.", WARN))
        elif key == "energy":
            self.db.econ_adjust(gid, uid, work_at=0)
            await ctx.reply(embed=embed("🥤 Wired", "Cooldown gone — you're ready for another `work` shift.", OK))
        elif key == "bag":
            got = open_bag(self.rng)
            self.db.econ_item_add(gid, uid, got, 1)
            await ctx.reply(embed=embed("🛍️ Mystery Bag", f"Inside: **{label(got)}**!", OK))
        elif key == "crate":
            rec = open_crate(self.rng)
            self.db.econ_item_add(gid, uid, rec, 1)
            _, title, rarity, value = RECORD_BY_KEY[rec]
            await ctx.reply(embed=embed(f"📦 {rarity.title()} find!", f"{RARITY_EMOJI[rarity]} **{title}**\n"
                                        f"Worth about {self.cash(value)} — `pawn` it for {self.cash(pawn_value(rec))}.",
                                        OK))
        elif it.buff:
            self.db.econ_buff(gid, uid, it.buff, now + it.hours * HOUR)
            extra = f"\n\n🥠 *“{self.rng.choice(FORTUNES)}”*" if key == "cookie" else ""
            emoji, text = BUFF_INFO[it.buff]
            await ctx.reply(embed=embed(f"{it.emoji} {it.name}", f"{emoji} **{text}** for "
                                        f"{it.hours:g} hour{'s' if it.hours != 1 else ''}.{extra}", OK))

    async def _paint(self, ctx, arg: str | None) -> None:
        colour = parse_hex(arg or "")
        if colour is None:
            await self.no(ctx, "Pick a colour", f"Give a hex colour, e.g. `{self.p(ctx)}use paintjob #ff3ea5`.")
            return
        guild, member = ctx.guild, ctx.author
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await self.no(ctx, "Sherwin Williams is closed", "SYKE needs **Manage Roles** to paint names. "
                          "Your Paint Job is safe in your inventory.")
            return
        roles = self.db.get_config(guild.id, "paint_roles")
        role = guild.get_role(int(roles.get(str(member.id), 0) or 0))
        try:
            if role is None:
                role = await guild.create_role(name=f"🎨 {member.display_name}"[:100], colour=discord.Colour(colour),
                                               reason="THE STRIP: paint job")
                self.db.update_config(guild.id, "paint_roles", **{str(member.id): role.id})
            else:
                await role.edit(colour=discord.Colour(colour), reason="THE STRIP: paint job")
            with contextlib.suppress(discord.HTTPException):   # sit just under SYKE so the colour shows
                await role.edit(position=max(1, me.top_role.position - 1))
            if role not in member.roles:
                await member.add_roles(role, reason="THE STRIP: paint job")
        except discord.HTTPException:
            await self.no(ctx, "The paint didn't take", "Discord refused the role change — your Paint Job is "
                          "still in your inventory.")
            return
        self.db.econ_item_add(guild.id, member.id, "paintjob", -1)
        await ctx.reply(embed=embed("🎨 Fresh coat", f"Your name is now `#{colour:06x}`. Looking expensive.",
                                    discord.Colour(colour)))

    @commands.hybrid_command(name="pawn", with_app_command=False, aliases=["sell"], description="Sell items or records at Pawn Deals")
    @app_commands.describe(item="What to sell, optionally followed by how many or 'all'")
    async def pawn(self, ctx: commands.Context, *, item: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        item, qty = split_tail(item, r"\d+|all")
        qty = qty or "1"
        gid, uid = ctx.guild.id, ctx.author.id
        held = self.db.econ_items(gid, uid)
        key = find_owned(item, held)
        have = held.get(key, 0) if key else 0
        if not have:
            await self.no(ctx, "You don't have that", f"Check `{self.p(ctx)}inventory`.")
            return
        n = have if qty.strip().lower() == "all" else (int(qty) if qty.strip().isdigit() else 0)
        if n <= 0 or n > have:
            await self.no(ctx, "How many?", f"You have {have}.")
            return
        each = pawn_value(key)
        total = each * n
        self.db.econ_item_add(gid, uid, key, -n)
        self.db.econ_adjust(gid, uid, cash=total)
        self.tip_store(gid, "pawn", int(total * OWNER_CUT), uid)
        await ctx.reply(embed=embed("🏷️ Pawned", f"{n}× {label(key)} → {self.cash(total)}.", OK))

    # ── crime & jail ──
    async def _criminal(self, ctx, w: dict, field: str, cooldown: float, verb: str) -> bool:
        """Common gate for crimes: not jailed, off cooldown. Replies and returns False if not."""
        if not await self.eco._free(ctx, w):
            return False
        if w[field] and time.time() - w[field] < cooldown:
            await self.no(ctx, "Lay low", f"Too hot to {verb} again. Try <t:{int(w[field] + cooldown)}:R>.")
            return False
        return True

    @commands.hybrid_command(name="rob", with_app_command=False, aliases=["mug"], description="Try to rob someone's cash on hand")
    @app_commands.describe(member="Who to rob")
    async def rob(self, ctx: commands.Context, member: discord.Member) -> None:
        w = await self.eco._guard(ctx)
        if w is None or not await self._criminal(ctx, w, "rob_at", ROB_COOLDOWN, "rob"):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        if member.bot or member.id == uid:
            await self.no(ctx, "Nice try", "Pick a real (other) person.")
            return
        t = self.db.econ_wallet(gid, member.id)
        if w["cash"] < ROB_MIN_CASH:
            await self.no(ctx, "You need bail money", f"Carry at least {self.cash(ROB_MIN_CASH)} — if you get "
                          "caught, you pay a fine.")
            return
        if t["cash"] < ROB_MIN_CASH:
            await self.no(ctx, "Not worth it", f"{member.display_name} has barely anything on them. "
                          "(The bank's off-limits.)")
            return
        now = time.time()
        self.db.econ_adjust(gid, uid, rob_at=now)
        if self.db.econ_items(gid, member.id).get("padlock"):
            self.db.econ_item_add(gid, member.id, "padlock", -1)
            fine = self.db.econ_move(gid, uid, member.id, rob_fine(w["cash"]))
            await ctx.reply(embed=embed("🔒 Padlocked", f"{member.mention}'s padlock held. You fumbled, got spotted "
                                        f"and paid them {self.cash(fine)} to keep quiet.", WARN))
            return
        buffs = self.buffs(gid, uid)
        if self.rng.random() < odds(ROB_CHANCE, "lucky" in buffs):
            take = self.db.econ_move(gid, member.id, uid, rob_take(self.rng, t["cash"]))
            await ctx.reply(embed=embed("💰 Clean getaway", f"You lifted {self.cash(take)} off {member.mention}. "
                                        "Should've banked it.", OK))
            return
        fine = self.db.econ_move(gid, uid, member.id, rob_fine(w["cash"]))
        note = ""
        if "foresight" not in buffs:
            until = self.jail(gid, uid, ROB_JAIL_MIN)
            note = f"\n🚔 Jailed until <t:{int(until)}:t> — `{self.p(ctx)}bail` to get out early."
        await ctx.reply(embed=embed("🚨 Caught red-handed", f"{member.mention} caught you. You paid them "
                                    f"{self.cash(fine)}.{note}", WARN))

    @commands.hybrid_command(name="crime", with_app_command=False, aliases=["scheme"], description="Pull a risky solo job — big payout or jail")
    async def crime(self, ctx: commands.Context) -> None:
        w = await self.eco._guard(ctx)
        if w is None or not await self._criminal(ctx, w, "crime_at", CRIME_COOLDOWN, "pull a job"):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        self.db.econ_adjust(gid, uid, crime_at=time.time())
        buffs = self.buffs(gid, uid)
        job = self.rng.choice(CRIMES)
        if self.rng.random() < odds(CRIME_CHANCE, "lucky" in buffs):
            pay = int(crime_payout(self.rng) * self.eco.happy(gid))
            self.db.econ_adjust(gid, uid, cash=pay)
            await ctx.reply(embed=embed("🕶️ Smooth", f"You {job} and walked away with {self.cash(pay)}.", OK))
            return
        fine = self.db.econ_move(gid, uid, None, crime_fine(self.rng))
        if "foresight" in buffs:
            await ctx.reply(embed=embed("🔮 Saw it coming", f"You tried to {job.split(' ', 1)[0]}… "
                                        f"{self.rng.choice(CAUGHT)}, but you slipped away. Lost {self.cash(fine)}.",
                                        WARN))
            return
        until = self.jail(gid, uid, crime_jail_minutes(self.rng))
        await ctx.reply(embed=embed("🚔 Busted", f"You {job}, but {self.rng.choice(CAUGHT)}. Fined "
                                    f"{self.cash(fine)} and jailed until <t:{int(until)}:t>.", WARN))

    @commands.hybrid_command(name="heist", with_app_command=False, description="Hit a storefront's register (owned stores only)")
    @app_commands.describe(store="Which storefront")
    async def heist(self, ctx: commands.Context, *, store: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None or not await self._criminal(ctx, w, "heist_at", HEIST_COOLDOWN, "pull a heist"):
            return
        gid, uid = ctx.guild.id, ctx.author.id
        s = find_store(store)
        row = self.db.econ_shop(gid, s.key) if s else None
        if s is None or row is None:
            await self.no(ctx, "Nothing to hit", "Only owned storefronts keep a register. See `property`.")
            return
        if row["owner_id"] == uid:
            await self.no(ctx, "That's your store", "Just `cashup`.")
            return
        now = time.time()
        till = pending(row, s, now)
        if till < HEIST_MIN:
            await self.no(ctx, "Register's empty", f"{s.name} only has {self.cash(till)} in the till. Not worth it.")
            return
        self.db.econ_adjust(gid, uid, heist_at=now)
        buffs = self.buffs(gid, uid)
        if self.rng.random() < odds(HEIST_CHANCE, "lucky" in buffs):
            take = till // 2
            # bank the register's accrual, then take half of it
            self.db.econ_shop_update(gid, s.key, register=till - take, collected_at=now)
            self.db.econ_adjust(gid, uid, cash=take)
            await ctx.reply(embed=embed("💼 Heist pulled", f"You cleaned out half of **{s.name}**'s register: "
                                        f"{self.cash(take)}. <@{row['owner_id']}> won't be happy.", OK))
            return
        fine = self.db.econ_move(gid, uid, row["owner_id"], 300)
        note = ""
        if "foresight" not in buffs:
            until = self.jail(gid, uid, HEIST_JAIL_MIN)
            note = f" Jailed until <t:{int(until)}:t>."
        await ctx.reply(embed=embed("🚨 Alarm tripped", f"**{s.name}**'s alarm went off. You paid the owner "
                                    f"{self.cash(fine)} in damages.{note}", WARN))

    @commands.hybrid_command(name="jail", with_app_command=False, aliases=["cell"], description="Who's locked up — or check one person")
    @app_commands.describe(member="Check someone (defaults to you)")
    async def jail_cmd(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        who = member or ctx.author
        ww = self.db.econ_wallet(ctx.guild.id, who.id)
        now = time.time()
        if ww["jail_until"] > now:
            cost = bail_cost(ww["jail_until"] - now)
            await ctx.reply(embed=embed("🚔 Locked up", f"{who.mention} is out <t:{int(ww['jail_until'])}:R>. "
                                        f"Bail is {self.cash(cost)} — `{self.p(ctx)}bail"
                                        f"{'' if who == ctx.author else ' @' + who.display_name}`.", WARN))
        else:
            await ctx.reply(embed=embed("🕊️ Free", f"{who.mention} is a free citizen of the Strip.", OK))

    @commands.hybrid_command(name="bail", with_app_command=False, description="Pay bail at Bail Bonds — for you or someone else")
    @app_commands.describe(member="Who to bail out (defaults to you)")
    async def bail(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        gid = ctx.guild.id
        who = member or ctx.author
        ww = self.db.econ_wallet(gid, who.id)
        now = time.time()
        if ww["jail_until"] <= now:
            await self.no(ctx, "Nobody to bail", f"{who.display_name} isn't locked up.")
            return
        cost = bail_cost(ww["jail_until"] - now)
        if not self.db.econ_spend(gid, ctx.author.id, cost):
            await self.no(ctx, "Can't afford bail", f"Bail is {self.cash(cost)}; you have {self.cash(w['cash'])}.")
            return
        self.db.econ_adjust(gid, who.id, jail_until=0)
        self.tip_store(gid, "bail", int(cost * BAIL_CUT), ctx.author.id)
        text = "You're out." if who == ctx.author else f"{who.mention} walks free, thanks to {ctx.author.mention}."
        await ctx.reply(embed=embed("🔓 Bail posted", f"Paid {self.cash(cost)} at Bail Bonds. {text}", OK))

    # ── property ──
    def _map_rows(self, guild) -> list[dict]:
        owned = self.db.econ_shops(guild.id)
        rows = []
        for s in STORES:
            o = owned.get(s.key)
            name = None
            if o:
                m = guild.get_member(o["owner_id"])
                name = getattr(m, "display_name", None) or f"#{o['owner_id']}"
            rows.append({"name": s.name, "colour": s.colour, "owner": name, "level": o["level"] if o else 0,
                         "price": s.price, "income": shop_income(s, o["level"] if o else 1)})
        return rows

    @commands.hybrid_group(name="property", aliases=["prop", "estate", "realty"], fallback="map",
                           case_insensitive=True, description="Own a piece of the Strip: the property map")
    async def property_group(self, ctx: commands.Context) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        p = self.p(ctx)
        from .economy import render
        image = await render(art.strip_map, self._map_rows(ctx.guild), filename="the-strip.png")
        mine = [s.name for s in STORES if (self.db.econ_shop(ctx.guild.id, s.key) or {}).get("owner_id") == ctx.author.id]
        body = (f"Buy a storefront and it earns every hour into its register (holds a day's takings). "
                f"Owners also get 10% of everything sold inside.\n`{p}property buy <store>` · `{p}property upgrade` · "
                f"`{p}cashup` · `{p}property sell`")
        if mine:
            body += f"\n\nYou own: **{', '.join(mine)}**"
        e = embed("🏙️ THE STRIP — Property", body, BRAND)
        if image is not None:
            e.set_image(url=f"attachment://{image.filename}")
        await self.eco.send(ctx, e, image)

    @property_group.command(name="buy", description="Buy an unowned storefront")
    @app_commands.describe(store="Which storefront")
    async def property_buy(self, ctx: commands.Context, *, store: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        s = find_store(store)
        if s is None:
            await self.no(ctx, "Which store?", f"See `{self.p(ctx)}property` for the map.")
            return
        row = self.db.econ_shop(ctx.guild.id, s.key)
        if row:
            await self.no(ctx, "Already owned", f"<@{row['owner_id']}> owns {s.name}.")
            return
        if not self.db.econ_shop_buy(ctx.guild.id, s.key, ctx.author.id, s.price, time.time()):
            await self.no(ctx, "Not enough cash", f"{s.name} costs {self.cash(s.price)} — you have "
                          f"{self.cash(w['cash'])} on hand. (`withdraw` from the bank first.)")
            return
        await ctx.reply(embed=embed(f"🔑 {s.name} is yours", f"It earns {self.cash(shop_income(s, 1))}/hour into its "
                                    f"register, plus 10% of everything sold there. `{self.p(ctx)}cashup` to empty the register.",
                                    OK))

    def _mine(self, ctx, store: str) -> tuple[Store, dict] | None:
        s = find_store(store)
        row = self.db.econ_shop(ctx.guild.id, s.key) if s else None
        if s is None or row is None or row["owner_id"] != ctx.author.id:
            return None
        return s, row

    @property_group.command(name="upgrade", description="Upgrade one of your storefronts (+50% income per level)")
    @app_commands.describe(store="Which of your storefronts")
    async def property_upgrade(self, ctx: commands.Context, *, store: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        found = self._mine(ctx, store)
        if found is None:
            await self.no(ctx, "Not yours", "You can only upgrade storefronts you own.")
            return
        s, row = found
        if row["level"] >= MAX_LEVEL:
            await self.no(ctx, "Maxed out", f"{s.name} is already level {MAX_LEVEL}.")
            return
        cost = upgrade_cost(s, row["level"])
        if not self.db.econ_spend(ctx.guild.id, ctx.author.id, cost):
            await self.no(ctx, "Not enough cash", f"Upgrading {s.name} costs {self.cash(cost)}.")
            return
        now = time.time()
        # lock in what the register earned at the old rate before the new rate kicks in
        self.db.econ_shop_update(ctx.guild.id, s.key, level=row["level"] + 1, invested=row["invested"] + cost,
                                 register=pending(row, s, now), collected_at=now)
        await ctx.reply(embed=embed(f"⭐ {s.name} → level {row['level'] + 1}",
                                    f"Now earns {self.cash(shop_income(s, row['level'] + 1))}/hour.", OK))

    @property_group.command(name="sell", description="Sell a storefront back to the bank (60% of what you put in)")
    @app_commands.describe(store="Which of your storefronts")
    async def property_sell(self, ctx: commands.Context, *, store: str) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        found = self._mine(ctx, store)
        if found is None:
            await self.no(ctx, "Not yours", "You can only sell storefronts you own.")
            return
        s, row = found
        value = int(row["invested"] * SELL_BACK) + pending(row, s, time.time())
        self.db.econ_shop_release(ctx.guild.id, s.key)
        self.db.econ_adjust(ctx.guild.id, ctx.author.id, cash=value)
        await ctx.reply(embed=embed("🏷️ Sold", f"{s.name} is back on the market. You got {self.cash(value)} "
                                    "(including what was in the register).", OK))

    @property_group.command(name="collect", description="Empty the registers of every storefront you own")
    async def property_collect(self, ctx: commands.Context) -> None:
        await self.cashup.callback(self, ctx)

    @commands.hybrid_command(name="cashup", with_app_command=False, aliases=["takings"], description="Empty your storefronts' registers")
    async def cashup(self, ctx: commands.Context) -> None:
        w = await self.eco._guard(ctx)
        if w is None:
            return
        now = time.time()
        lines, total = [], 0
        for key, row in self.db.econ_shops(ctx.guild.id).items():
            if row["owner_id"] != ctx.author.id:
                continue
            s = STORE_BY_KEY[key]
            amount = pending(row, s, now)
            self.db.econ_shop_update(ctx.guild.id, key, register=0, collected_at=now)
            total += amount
            lines.append(f"{s.name} · {self.cash(amount)}")
        if not lines:
            await self.no(ctx, "No storefronts", f"Buy one with `{self.p(ctx)}property buy <store>`.")
            return
        self.db.econ_adjust(ctx.guild.id, ctx.author.id, cash=total)
        await ctx.reply(embed=embed("🧾 Registers emptied", "\n".join(lines) + f"\n\n**Total: {self.cash(total)}**", OK))


def help_lines(prefix: str) -> list[str]:
    return [
        f"`{prefix}shop` / `{prefix}buy` / `{prefix}use` — the storefronts",
        f"`{prefix}rob` / `{prefix}crime` / `{prefix}heist` — crime (watch for jail)",
        f"`{prefix}property` — own a piece of the Strip",
    ]
