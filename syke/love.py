"""Love on THE STRIP, from Discord chat: `!marry`, `!divorce`, `!partner`, `!gift` and the chapel's shelf in `!shop`.

The game (THE STRIP, the `hometown` server) keeps everyone's relationships, so a marriage made here is the
same marriage in the game and the other way round. This is the simple version of the game's love system:

* **Marry:** `!marry @someone [ring]` posts a proposal. Nothing happens until *they* press Yes. Then you're
  married, here and on the Strip. No dating ladder, no chapel booking (the game still has those).
* **Divorce:** one-sided, like every ending on the Strip. Ends whatever you have (marriage, engagement, dating).
* **Rings and gifts** are paid from this server's own wallet (`$bal`), and land in the game: the ring is on
  your finger there, the gift in their pocket. The bottle-cap ring and a daily wildflower are free, so love
  never needs money. A ring you already own in the game is used instead of buying a new one.

Money is taken only at the moment it's needed (when they say yes, when the gift is sent) and handed back
if the game says no, so a failed call never costs anything. Off until `SYKE_STRIP_GAME_URL` and
`SYKE_STRIP_API_SECRET` (the same secret the game uses for SYKE) are set.

    game:  POST <SYKE_STRIP_GAME_URL>/bot/api/<op>   Authorization: Bearer <secret>
    ops:   catalog, status, check, marry, divorce, gift
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, OK, WARN, embed

log = logging.getLogger(__name__)

LOVE = discord.Colour(0xFF4F9A)
GAME_TIMEOUT = 6            # seconds to wait for the game server
PROPOSAL_SECONDS = 120      # how long a proposal waits for an answer
CONFIRM_SECONDS = 30

# The chapel's shelf: a copy of the game's shared/relationships.js (RINGS, GIFTS). The live catalogue
# replaces it the first time the game answers, so prices always match the game's.
RINGS: dict[str, dict] = {
    "paper": {"name": "Bottle-cap ring (handmade)", "price": 0},
    "promise": {"name": "Silver promise ring", "price": 250},
    "gold": {"name": "Gold band", "price": 2500},
    "diamond": {"name": "Diamond solitaire", "price": 15000},
}
GIFTS: dict[str, dict] = {
    "flower": {"name": "A wildflower", "icon": "🌼", "price": 0, "daily": True},
    "candy": {"name": "Box of chocolates", "icon": "🍫", "price": 25},
    "roses": {"name": "Dozen roses", "icon": "🌹", "price": 60},
    "teddy": {"name": "Teddy bear", "icon": "🧸", "price": 45},
    "perfume": {"name": "Strip No. 5 perfume", "icon": "💐", "price": 150},
    "watch": {"name": "Gold watch", "icon": "⌚", "price": 900},
}
RING_WORDS = {"paper": ["bottle", "bottlecap", "cap", "free", "handmade"], "promise": ["silver"],
              "gold": ["band"], "diamond": ["solitaire", "rock"]}
CHAPEL_WORDS = {"chapel", "love", "rings", "ring", "gifts", "gift", "wedding", "little chapel"}
ENDED = {"divorce": ("💔 Divorced", "are divorced"), "calloff": ("💔 Engagement off", "called off the engagement"),
         "breakup": ("💔 Broken up", "broke up")}
OFFLINE = {"ok": False, "error": "game_down", "message": "THE STRIP isn't answering right now. Try again in a bit."}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def find_ring(text: str | None, rings: dict | None = None) -> str | None:
    """'gold', 'Gold band', 'silver', 'diamond ring' -> a ring key. Nothing given means the free one."""
    rings = rings or RINGS
    if not text or not text.strip():
        return "paper"
    want = _norm(text.replace("ring", ""))
    for key, r in rings.items():
        words = [key, r["name"], *RING_WORDS.get(key, [])]
        if any(want and (want == _norm(w) or _norm(w).startswith(want)) for w in words):
            return key
    return None


def find_gift(text: str | None, gifts: dict | None = None) -> str | None:
    gifts = gifts or GIFTS
    want = _norm(text or "")
    if not want:
        return None
    for key, g in gifts.items():
        if want in (_norm(key), _norm(g["name"])) or _norm(g["name"]).startswith(want) or key.startswith(want):
            return key
    for key, g in gifts.items():   # "chocolate", "bear", "rose"
        if want.rstrip("s") and want.rstrip("s") in _norm(g["name"]):
            return key
    return None


def names_of(*members) -> dict[str, str]:
    return {str(m.id): getattr(m, "display_name", None) or str(m.id) for m in members if m is not None}


class GameClient:
    """Talks to the game's /bot/api. Never raises: a down game is just an `ok: false` answer."""

    def __init__(self, url: str, secret: str) -> None:
        self.url, self.secret = url.rstrip("/"), secret
        self._session = None

    async def call(self, op: str, **body) -> dict:
        import aiohttp

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=GAME_TIMEOUT))
            async with self._session.post(f"{self.url}/bot/api/{op}", json=body,
                                          headers={"Authorization": f"Bearer {self.secret}"}) as r:
                if r.status == 401:
                    log.error("THE STRIP rejected SYKE's secret: SYKE_STRIP_API_SECRET must match the game's SYKE_API_SECRET")
                    return {"ok": False, "error": "game_auth", "message": "SYKE and THE STRIP disagree on the API secret."}
                data = await r.json(content_type=None)
                return data if isinstance(data, dict) else dict(OFFLINE)
        except Exception as err:   # down, slow, or not JSON
            log.warning("THE STRIP %s failed: %r", op, err)
            return dict(OFFLINE)

    async def close(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()


class Chapel:
    """The money side of love, kept away from Discord so it can be tested: charge, ask the game, refund on a no."""

    def __init__(self, storage, game) -> None:
        self.db, self.game = storage, game
        self.rings, self.gifts = dict(RINGS), dict(GIFTS)
        self._loaded = False

    async def catalog(self) -> None:
        if self._loaded:
            return
        c = await self.game.call("catalog")
        if c.get("ok") and isinstance(c.get("rings"), dict) and isinstance(c.get("gifts"), dict):
            self.rings, self.gifts, self._loaded = c["rings"], c["gifts"], True

    async def quote(self, gid: int, a, b) -> dict:
        """Can these two marry, and what would the ring cost? Asked before the proposal is shown."""
        return await self.game.call("check", user_id=str(a.id), to=str(b.id), names=names_of(a, b))

    def ring_price(self, ring: str, owned: dict) -> int:
        return 0 if (owned or {}).get(ring) else int(self.rings.get(ring, {}).get("price", 0))

    async def wed(self, gid: int, a, b, ring: str) -> dict:
        """They said yes. Re-check, take the ring money (if any), marry them in the game, refund if it says no."""
        chk = await self.quote(gid, a, b)
        if not chk.get("ok"):
            return chk
        owned = bool((chk.get("rings") or {}).get(ring))
        price = self.ring_price(ring, chk.get("rings") or {})
        if price and not self.db.econ_spend(gid, a.id, price):
            return {"ok": False, "error": "broke", "message": f"{a.display_name} can't cover the ring any more ({price:,})."}
        out = await self.game.call("marry", user_id=str(a.id), to=str(b.id), ring=ring, use_owned=owned, names=names_of(a, b))
        if not out.get("ok") and price:
            self.db.econ_adjust(gid, a.id, cash=price)
        return {**out, "paid": price if out.get("ok") else 0, "owned": owned}

    async def send_gift(self, gid: int, a, b, key: str) -> dict:
        price = int(self.gifts.get(key, {}).get("price", 0))
        if price and not self.db.econ_spend(gid, a.id, price):
            return {"ok": False, "error": "broke", "message": f"That costs {price:,} and you don't have it on you.", "price": price}
        out = await self.game.call("gift", user_id=str(a.id), to=str(b.id), gift=key, names=names_of(a, b))
        if not out.get("ok") and price:
            self.db.econ_adjust(gid, a.id, cash=price)
        return {**out, "paid": price if out.get("ok") else 0}


# ── buttons ─────────────────────────────────────────────────────────────────
class ProposalView(discord.ui.View):
    def __init__(self, cog: "Love", ctx, member, ring: str, price: int) -> None:
        super().__init__(timeout=PROPOSAL_SECONDS)
        self.cog, self.ctx, self.member, self.ring, self.price = cog, ctx, member, ring, price
        self.message: discord.Message | None = None
        self.done = False
        self.lock = asyncio.Lock()

    async def _finish(self, interaction, e) -> None:
        self.done = True
        self.stop()
        await interaction.edit_original_response(embed=e, view=None)

    @discord.ui.button(label="Yes 💍", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This proposal isn't for you. 👀", ephemeral=True)
            return
        async with self.lock:
            if self.done:
                return
            await interaction.response.defer()
            out = await self.cog.chapel.wed(self.ctx.guild.id, self.ctx.author, self.member, self.ring)
            await self._finish(interaction, self.cog.wed_embed(self.ctx, self.member, self.ring, out))

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        who = interaction.user.id
        if who not in (self.member.id, self.ctx.author.id):
            await interaction.response.send_message("This proposal isn't for you. 👀", ephemeral=True)
            return
        async with self.lock:
            if self.done:
                return
            await interaction.response.defer()
            text = (f"{self.member.mention} said no. Nobody paid a thing." if who == self.member.id
                    else f"{self.ctx.author.mention} took the proposal back.")
            await self._finish(interaction, embed("💔 Not this time", text, WARN))

    async def on_timeout(self) -> None:
        if self.done or self.message is None:
            return
        self.done = True
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(embed=embed("⌛ No answer", f"{self.member.mention} never answered. The ring stays in "
                                                f"{self.ctx.author.mention}'s pocket.", WARN), view=None)


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, on_yes) -> None:
        super().__init__(timeout=CONFIRM_SECONDS)
        self.author_id, self.on_yes = author_id, on_yes
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("That's not your decision to make.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, end it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=await self.on_yes(), view=None)

    @discord.ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(embed=embed("Phew", "Nothing changed.", OK), view=None)

    async def on_timeout(self) -> None:
        if not self.done and self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=None)


# ── the cog ─────────────────────────────────────────────────────────────────
class Love(commands.Cog, name="Love"):
    """Marriage, divorce and gifts, shared with THE STRIP game."""

    love = app_commands.Group(name="love", description="THE STRIP love: marry, divorce, gifts", guild_only=True)

    def __init__(self, bot, game=None) -> None:
        self.bot = bot
        s = bot.settings
        url = getattr(s, "strip_game_url", "")
        self.game = game or (GameClient(url, s.strip_api_secret) if url and s.strip_api_secret else None)
        self.chapel = Chapel(bot.storage, self.game) if self.game else None

    async def cog_unload(self) -> None:
        if isinstance(self.game, GameClient):
            await self.game.close()

    @property
    def eco(self):
        return self.bot.get_cog("Economy")

    def p(self, ctx) -> str:
        return self.bot.display_prefix(ctx.guild.id)

    def cash(self, n: int) -> str:
        return self.eco.money(n) if self.eco else f"**{n:,}**"

    async def no(self, ctx, title: str, body: str) -> None:
        await ctx.reply(embed=embed(title, body, WARN), ephemeral=True)

    async def ready(self, ctx) -> bool:
        if ctx.guild is None:
            await self.no(ctx, "Servers only", "Love on the Strip happens inside a server.")
            return False
        if self.chapel is None:
            await self.no(ctx, "💒 The chapel is closed", "SYKE isn't linked to THE STRIP game here yet. The bot owner "
                          "sets `SYKE_STRIP_GAME_URL` (and the shared `SYKE_STRIP_API_SECRET`).")
            return False
        await self.chapel.catalog()
        return True

    async def failed(self, ctx, out: dict) -> None:
        await self.no(ctx, "💔 Can't do that", out.get("message") or "Something went wrong. Try again in a bit.")

    def ring_label(self, key: str) -> str:
        r = self.chapel.rings.get(key, {})
        return f"💍 **{r.get('name', key)}** · " + ("free" if not r.get("price") else self.cash(r["price"]))

    # ── shown inside !shop ──
    def is_chapel(self, text: str | None) -> bool:
        return self.chapel is not None and (text or "").strip().lower() in CHAPEL_WORDS

    def chapel_embed(self, p: str) -> discord.Embed:
        rings = "\n".join(f"{self.ring_label(k)} · `{k}`" for k in self.chapel.rings)
        gifts = "\n".join(f"{g.get('icon', '🎁')} **{g['name']}** · "
                          f"{'free, once a day' if g.get('daily') else self.cash(g['price'])} · `{k}`"
                          for k, g in self.chapel.gifts.items())
        e = embed("💒 The Little Chapel", "Rings and gifts, paid from your cash here. They show up in THE STRIP game "
                  "too: the ring on your finger, the gift in their pocket.", LOVE)
        e.add_field(name="Rings", value=rings, inline=False)
        e.add_field(name="Gifts", value=gifts, inline=False)
        e.set_footer(text=f"{p}marry @someone <ring>  •  {p}gift @someone <gift>  •  {p}partner  •  {p}divorce")
        return e

    def shop_field(self, e: discord.Embed, p: str) -> None:
        if self.chapel is None:
            return
        rings = [f"💍 `{k}` " + (f"{r['price']:,}" if r.get("price") else "free") for k, r in self.chapel.rings.items()]
        e.add_field(name="💒 Little Chapel", value="\n".join(rings + [f"🎁 gifts: `{p}shop chapel`"]), inline=True)

    def wed_embed(self, ctx, member, ring: str, out: dict) -> discord.Embed:
        if not out.get("ok"):
            return embed("💔 The wedding's off", out.get("message") or "Something went wrong. Nobody paid anything.", WARN)
        how = ("their own " if out.get("owned") else "") + self.chapel.rings.get(ring, {}).get("name", "a ring")
        paid = f" ({self.cash(out['paid'])})" if out.get("paid") else ""
        return embed("💒 Just married!", f"{ctx.author.mention} and {member.mention} are married, with {how}{paid}.\n\n"
                     f"It's official on THE STRIP too: rings on, and the chapel's photo moment waits for you in the game. "
                     f"`{self.p(ctx)}partner` shows your marriage.", LOVE)

    # ── commands ──
    @commands.hybrid_command(name="marry", with_app_command=False, aliases=["propose"],
                             description="Propose to someone (married here and in THE STRIP)")
    async def marry(self, ctx: commands.Context, member: discord.Member, *, ring: str | None = None) -> None:
        if not await self.ready(ctx):
            return
        p = self.p(ctx)
        if member.bot:
            await self.no(ctx, "🤖 Bots can't say yes", "Pick a human.")
            return
        if member.id == ctx.author.id:
            await self.no(ctx, "Self-love is great", "But you can't marry yourself.")
            return
        key = find_ring(ring, self.chapel.rings)
        if key is None:
            await self.no(ctx, "Which ring?", f"See the rings with `{p}shop chapel`, e.g. `{p}marry @someone gold`. "
                          "No ring named means the free bottle-cap ring.")
            return
        chk = await self.chapel.quote(ctx.guild.id, ctx.author, member)
        if not chk.get("ok"):
            await self.failed(ctx, chk)
            return
        price = self.chapel.ring_price(key, chk.get("rings") or {})
        if price:
            w = await self.eco._guard(ctx) if self.eco else None
            if w is None:
                return
            if w["cash"] < price:
                await self.no(ctx, "Not enough cash", f"The {self.chapel.rings[key]['name']} costs {self.cash(price)}, you "
                              f"have {self.cash(w['cash'])} on hand. The bottle-cap ring is free: `{p}marry @{member.display_name}`.")
                return
        cost = "It's yours already." if (chk.get("rings") or {}).get(key) else (
            f"You pay {self.cash(price)} only if they say yes." if price else "It's free.")
        e = embed("💍 A proposal!", f"{ctx.author.mention} is down on one knee: **{member.display_name}, will you marry me?**\n\n"
                  f"{self.ring_label(key)}. {cost}\nSaying yes makes it official here and on THE STRIP.", LOVE)
        e.set_footer(text=f"{member.display_name} has {PROPOSAL_SECONDS // 60} minutes to answer")
        view = ProposalView(self, ctx, member, key, price)
        view.message = await ctx.reply(content=member.mention, embed=e, view=view)

    @commands.hybrid_command(name="divorce", with_app_command=False, aliases=["breakup"],
                             description="End your marriage or relationship (here and in THE STRIP)")
    async def divorce(self, ctx: commands.Context) -> None:
        if not await self.ready(ctx):
            return
        st = await self.chapel.game.call("status", user_id=str(ctx.author.id), viewer=str(ctx.author.id))
        if not st.get("ok"):
            await self.failed(ctx, st)
            return
        if not st.get("partner"):
            await self.no(ctx, "Nothing to end", "You're not with anyone right now.")
            return
        partner = st["partner"]

        async def go() -> discord.Embed:
            out = await self.chapel.game.call("divorce", user_id=str(ctx.author.id))
            if not out.get("ok"):
                return embed("Can't do that", out.get("message") or "Something went wrong.", WARN)
            title, verb = ENDED.get(out.get("kind"), ("💔 Over", "split up"))
            return embed(title, f"{ctx.author.mention} and <@{out['with']['id']}> {verb}. It's the same on THE STRIP.", WARN)

        what = {"married": "divorce", "separated": "divorce", "engaged": "call off the engagement with"}.get(st.get("status"), "break up with")
        view = ConfirmView(ctx.author.id, go)
        view.message = await ctx.reply(embed=embed("💔 Sure?", f"You're about to {what} <@{partner['id']}>. "
                                                   "Only you need to decide, and it's the same in THE STRIP game.", WARN),
                                       view=view, ephemeral=True)

    @commands.hybrid_command(name="partner", with_app_command=False, aliases=["spouse", "relationship", "married"],
                             description="Who someone is with (from THE STRIP)")
    async def partner(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if not await self.ready(ctx):
            return
        who = member or ctx.author
        st = await self.chapel.game.call("status", user_id=str(who.id), viewer=str(ctx.author.id), names=names_of(who))
        if not st.get("ok"):
            await self.failed(ctx, st)
            return
        if st.get("hidden"):
            await ctx.reply(embed=embed(f"🔒 {who.display_name}", "Keeps their love life private.", BRAND))
            return
        pt = st.get("partner")
        if not pt:
            body = f"**{st.get('personal', 'Single')}.**"
            if who.id == ctx.author.id:
                body += f"\n\n`{self.p(ctx)}marry @someone` to propose (the bottle-cap ring is free)."
            await ctx.reply(embed=embed(f"💌 {who.display_name}", body, LOVE))
            return
        joiner = {"married": " to", "engaged": " to", "dating": "", "separated": " from"}.get(st.get("status"), " with")
        lines = [f"**{st.get('label', st.get('personal'))}**{joiner} <@{pt['id']}>"]
        if st.get("married_at"):
            lines.append(f"💒 Married <t:{int(st['married_at'])}:D> (<t:{int(st['married_at'])}:R>)")
        elif st.get("engaged_at"):
            lines.append(f"💍 Engaged <t:{int(st['engaged_at'])}:R>")
        elif st.get("dating_since"):
            lines.append(f"💕 Together since <t:{int(st['dating_since'])}:D>")
        if st.get("ring_name"):
            lines.append(f"💍 {st['ring_name']}")
        extra = []
        if st.get("dates"):
            extra.append(f"{st['dates']} date{'s' if st['dates'] != 1 else ''}")
        if st.get("time_together", 0) >= 60:
            extra.append(f"{st['time_together'] // 3600}h {st['time_together'] % 3600 // 60}m together on the Strip")
        if st.get("gifts"):
            extra.append(f"{st['gifts']} gift{'s' if st['gifts'] != 1 else ''}")
        if extra:
            lines.append(" · ".join(extra))
        if pt.get("online"):
            lines.append(f"🟢 {pt.get('name', 'Their partner')} is on THE STRIP right now")
        await ctx.reply(embed=embed(f"💞 {who.display_name}", "\n".join(lines), LOVE))

    @commands.hybrid_command(name="gift", with_app_command=False, aliases=["present"],
                             description="Send someone a gift (it lands in THE STRIP game)")
    async def gift(self, ctx: commands.Context, member: discord.Member | None = None, *, gift: str | None = None) -> None:
        if not await self.ready(ctx):
            return
        p = self.p(ctx)
        key = find_gift(gift, self.chapel.gifts)
        if member is None or key is None:
            await ctx.reply(embed=self.chapel_embed(p))
            return
        if member.bot or member.id == ctx.author.id:
            await self.no(ctx, "Gifts are for other people", "Pick someone else.")
            return
        g = self.chapel.gifts[key]
        if g.get("price"):
            if (await self.eco._guard(ctx) if self.eco else None) is None:
                return
        out = await self.chapel.send_gift(ctx.guild.id, ctx.author, member, key)
        if not out.get("ok"):
            await self.failed(ctx, out)
            return
        paid = f" for {self.cash(out['paid'])}" if out.get("paid") else ""
        await ctx.reply(embed=embed(f"{g.get('icon', '🎁')} Gift sent", f"{ctx.author.mention} gave {member.mention} "
                                    f"**{g['name']}**{paid}. It's waiting in their pocket on THE STRIP.", LOVE))

    # ── /love ──
    async def run(self, interaction: discord.Interaction, command: str, *args, **kwargs) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        await getattr(self, command).callback(self, ctx, *args, **kwargs)

    @love.command(name="marry", description="Propose to someone (married here and in THE STRIP)")
    @app_commands.describe(member="Who to propose to", ring="paper (free), promise, gold or diamond")
    async def s_marry(self, interaction: discord.Interaction, member: discord.Member, ring: str | None = None) -> None:
        await self.run(interaction, "marry", member, ring=ring)

    @love.command(name="divorce", description="End your marriage or relationship")
    async def s_divorce(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "divorce")

    @love.command(name="partner", description="Who someone is with")
    @app_commands.describe(member="Whose love life (defaults to you)")
    async def s_partner(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.run(interaction, "partner", member)

    @love.command(name="gift", description="Send someone a gift (it lands in THE STRIP game)")
    @app_commands.describe(member="Who it's for", gift="flower (free daily), candy, roses, teddy, perfume or watch")
    async def s_gift(self, interaction: discord.Interaction, member: discord.Member, gift: str) -> None:
        await self.run(interaction, "gift", member, gift=gift)

    @love.command(name="chapel", description="Rings and gifts for sale")
    async def s_chapel(self, interaction: discord.Interaction) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        if await self.ready(ctx):
            await ctx.reply(embed=self.chapel_embed(self.p(ctx)))
