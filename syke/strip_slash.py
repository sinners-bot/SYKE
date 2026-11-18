"""Slash commands for THE STRIP, grouped as `/strip …` and `/casino …`.

Discord caps a bot at 100 top-level slash commands, and SYKE is close to it. So
the economy's prefix commands stay flat (`!daily`, `!blackjack 100`) while their
slash versions live under two groups. Every subcommand here just builds a
context from the interaction and runs the exact same callback as the prefix
command, so the two can never drift apart.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class StripSlash(commands.Cog, name="StripSlash"):
    strip = app_commands.Group(name="strip", description="THE STRIP: earn, shop, crime and your wallet",
                               guild_only=True)
    casino = app_commands.Group(name="casino", description="THE STRIP casino: blackjack, slots, roulette, Hi-Lo",
                                guild_only=True)

    def __init__(self, bot) -> None:
        self.bot = bot

    async def run(self, interaction: discord.Interaction, cog: str, command: str, *args, **kwargs) -> None:
        target = self.bot.get_cog(cog)
        ctx = await commands.Context.from_interaction(interaction)
        await getattr(target, command).callback(target, ctx, *args, **kwargs)

    # ── /strip ──
    @strip.command(name="play", description="Show off THE STRIP game, with screenshots and a Play button")
    async def s_play(self, interaction: discord.Interaction) -> None:
        feed = self.bot.get_cog("StripFeed")
        ctx = await commands.Context.from_interaction(interaction)
        await feed.showcase(ctx)

    @strip.command(name="feed", description="Post Strip highlights from this server's players in a channel")
    @app_commands.describe(channel="Where to post (leave empty to see the current setting)", off="Turn the feed off")
    @app_commands.default_permissions(manage_guild=True)
    async def s_feed(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, off: bool = False) -> None:
        feed = self.bot.get_cog("StripFeed")
        ctx = await commands.Context.from_interaction(interaction)
        await feed.set_feed(ctx, channel, "off" if off else None)

    @strip.command(name="balance", description="Your Players Club card: cash, chips, bank and net worth")
    @app_commands.describe(member="Whose card (defaults to you)")
    async def s_balance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.run(interaction, "Economy", "balance", member)

    @strip.command(name="daily", description="Claim your daily cash (build a streak for more)")
    async def s_daily(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "Economy", "daily")

    @strip.command(name="work", description="Clock a shift on the Strip for cash")
    async def s_work(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "Economy", "work")

    @strip.command(name="hustle", description="A risky hustle — could pay off, could backfire")
    async def s_hustle(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "Economy", "hustle_cmd")

    @strip.command(name="pay", description="Send cash to someone (small tax applies)")
    @app_commands.describe(member="Who to pay", amount="How much (a number, or all/half)")
    async def s_pay(self, interaction: discord.Interaction, member: discord.Member, amount: str) -> None:
        await self.run(interaction, "Economy", "pay", member, amount)

    @strip.command(name="deposit", description="Move cash into your bank (safe from robbers)")
    @app_commands.describe(amount="How much (a number, or all/half)")
    async def s_deposit(self, interaction: discord.Interaction, amount: str) -> None:
        await self.run(interaction, "Economy", "deposit", amount)

    @strip.command(name="withdraw", description="Take cash out of your bank")
    @app_commands.describe(amount="How much (a number, or all/half)")
    async def s_withdraw(self, interaction: discord.Interaction, amount: str) -> None:
        await self.run(interaction, "Economy", "withdraw", amount)

    @strip.command(name="rich", description="Richest on the Strip")
    async def s_rich(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "Economy", "rich")

    @strip.command(name="shop", description="Browse the Strip's storefronts")
    @app_commands.describe(store="One storefront to look inside (optional)")
    async def s_shop(self, interaction: discord.Interaction, store: str | None = None) -> None:
        await self.run(interaction, "StripWorld", "shop", store=store)

    @strip.command(name="buy", description="Buy an item from a storefront")
    @app_commands.describe(item="What to buy", qty="How many (default 1)")
    async def s_buy(self, interaction: discord.Interaction, item: str, qty: int = 1) -> None:
        await self.run(interaction, "StripWorld", "buy", item=f"{item} {max(1, qty)}")

    @strip.command(name="inventory", description="Your items and active buffs")
    @app_commands.describe(member="Whose inventory (defaults to you)")
    async def s_inventory(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.run(interaction, "StripWorld", "inventory", member)

    @strip.command(name="use", description="Use an item from your inventory")
    @app_commands.describe(item="What to use", arg="Extra info, e.g. a colour for a paint job")
    async def s_use(self, interaction: discord.Interaction, item: str, arg: str | None = None) -> None:
        await self.run(interaction, "StripWorld", "use", item=item, arg=arg)

    @strip.command(name="pawn", description="Sell items or records at Pawn Deals")
    @app_commands.describe(item="What to sell", qty="How many, or all (default 1)")
    async def s_pawn(self, interaction: discord.Interaction, item: str, qty: str = "1") -> None:
        await self.run(interaction, "StripWorld", "pawn", item=f"{item} {qty}")

    @strip.command(name="rob", description="Try to rob someone's cash on hand")
    @app_commands.describe(member="Who to rob")
    async def s_rob(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.run(interaction, "StripWorld", "rob", member)

    @strip.command(name="crime", description="Pull a risky solo job — big payout or jail")
    async def s_crime(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "StripWorld", "crime")

    @strip.command(name="heist", description="Hit a storefront's register")
    @app_commands.describe(store="Which storefront")
    async def s_heist(self, interaction: discord.Interaction, store: str) -> None:
        await self.run(interaction, "StripWorld", "heist", store=store)

    @strip.command(name="jail", description="Check who's locked up")
    @app_commands.describe(member="Check someone (defaults to you)")
    async def s_jail(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.run(interaction, "StripWorld", "jail_cmd", member)

    @strip.command(name="bail", description="Pay bail — for you or someone else")
    @app_commands.describe(member="Who to bail out (defaults to you)")
    async def s_bail(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self.run(interaction, "StripWorld", "bail", member)

    @strip.command(name="cashup", description="Empty your storefronts' registers")
    async def s_cashup(self, interaction: discord.Interaction) -> None:
        await self.run(interaction, "StripWorld", "cashup")

    # ── /casino ──
    @casino.command(name="blackjack", description="Real Vegas Strip blackjack — 3:2, dealer stands on 17")
    @app_commands.describe(bet="Chips to bet (a number, or all/half)")
    async def c_blackjack(self, interaction: discord.Interaction, bet: str) -> None:
        await self.run(interaction, "Economy", "blackjack", bet)

    @casino.command(name="slots", description="Pull the neon slots")
    @app_commands.describe(bet="Chips to bet (a number, or all/half)")
    async def c_slots(self, interaction: discord.Interaction, bet: str) -> None:
        await self.run(interaction, "Economy", "slots", bet)

    @casino.command(name="roulette", description="A number 0-36, or red/black/even/odd/low/high")
    @app_commands.describe(space="A number 0-36, or red/black/even/odd/low/high", bet="Chips to bet")
    async def c_roulette(self, interaction: discord.Interaction, space: str, bet: str) -> None:
        await self.run(interaction, "Economy", "roulette", space, bet)

    @casino.command(name="hilo", description="Higher or lower? Cash out before you miss")
    @app_commands.describe(bet="Chips to bet (a number, or all/half)")
    async def c_hilo(self, interaction: discord.Interaction, bet: str) -> None:
        await self.run(interaction, "Economy", "hilo", bet)

    @casino.command(name="flip", description="Coinflip — heads or tails, pays 1.95×")
    @app_commands.describe(side="heads or tails", bet="Chips to bet")
    @app_commands.choices(side=[app_commands.Choice(name="Heads", value="heads"),
                                app_commands.Choice(name="Tails", value="tails")])
    async def c_flip(self, interaction: discord.Interaction, side: str, bet: str) -> None:
        await self.run(interaction, "Economy", "flip", side, bet)

    @casino.command(name="chips", description="Swap cash for chips at the cage (free)")
    @app_commands.describe(amount="Cash to swap (a number, or all/half)")
    async def c_chips(self, interaction: discord.Interaction, amount: str) -> None:
        await self.run(interaction, "Economy", "chips", amount)

    @casino.command(name="cashout", description="Swap chips back into cash (free)")
    @app_commands.describe(amount="Chips to swap (default all)")
    async def c_cashout(self, interaction: discord.Interaction, amount: str = "all") -> None:
        await self.run(interaction, "Economy", "cashout", amount)
