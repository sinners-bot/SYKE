"""THE STRIP's feed: highlights from the game posted in each server, a Play button, and the live player count.

The game server POSTs to /strip/api/feed (same secret as the rest of the Strip API):
  {"kind": "online", "online": 42, "app_id": "..."}                    -> SYKE's status shows the count
  {"kind": "record"|"jackpot"|"wedding"|"cup"|"promotion"|"newplayer",
   "guild_id": "<the server the player launched from>", "player": {"id", "name"}, "data": {...}, "online": n}
Highlights are only posted where a server's admins chose a channel (`!stripfeed #channel` or `/strip feed`),
at most one every 90 seconds and 25 a day per server. Nothing is stored about the players.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, OK, WARN, embed

log = logging.getLogger("syke.strip_feed")
FEED_KEY = "strip_feed"
GAP = 90            # seconds between posts in one server
PER_DAY = 25        # posts per server per UTC day
GOLD = discord.Colour.from_str("#F5C542")
KINDS = {
    "record": ("🥇 New arcade record", "**{name}** just set the all-time record on **{game}** at Pixel Palace: **{score}**. Think you can beat it?"),
    "jackpot": ("🎰 Big win at the casino", "**{name}** {won} Who's next?"),
    "wedding": ("💒 Wedding bells on the Strip", "**{name}** and **{partner}** just got married at the chapel. Congratulations!"),
    "cup": ("🏆 Weekly cup champion", "**{name}** won this week's **{game}** cup at Pixel Palace."),
    "promotion": ("💼 Promotion", "**{name}** is now **{title}** ({job}, level {level})."),
    "newplayer": ("🌆 New arrival", "**{name}** from this server just moved to THE STRIP. Come say hi!"),
}


def launch_url(app_id: str) -> str | None:
    return f"https://discord.com/activities/{app_id}" if app_id and app_id.isdigit() else None


def play_view(app_id: str) -> discord.ui.View | None:
    url = launch_url(app_id)
    if not url:
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Play THE STRIP", emoji="🎰", url=url))
    return v


class StripFeed(commands.Cog, name="StripFeed"):
    """Strip highlights in your server, the Play button and the live count."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.online = 0
        self.app_id = ""
        self._presence = (None, 0.0)       # (text, when)
        self._sent: dict[int, list[float]] = {}

    # ---------------------------------------------------------------- from the game server
    async def handle(self, body: dict) -> dict:
        app_id = str(body.get("app_id") or "")
        if app_id.isdigit():
            self.app_id = app_id
        try:
            self.online = max(0, min(100000, int(body.get("online", self.online) or 0)))
        except (TypeError, ValueError):
            pass
        await self._update_presence()
        kind = str(body.get("kind") or "")
        if kind == "online":
            return {"ok": True}
        if kind not in KINDS:
            return {"ok": False, "error": "bad_kind"}
        try:
            gid = int(body.get("guild_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_ids"}
        guild = self.bot.get_guild(gid) if gid else None
        if guild is None:
            return {"ok": True, "posted": False, "why": "not_here"}
        cfg = self.bot.storage.get_config(gid, FEED_KEY)
        channel = guild.get_channel(int(cfg.get("channel") or 0)) if cfg.get("channel") else None
        if channel is None or cfg.get("off"):
            return {"ok": True, "posted": False, "why": "no_channel"}
        if kind in set(cfg.get("mute") or []):
            return {"ok": True, "posted": False, "why": "muted_kind"}
        now = time.time()
        sent = [t for t in self._sent.get(gid, []) if now - t < 86400]
        if (sent and now - sent[-1] < GAP) or len(sent) >= PER_DAY:
            self._sent[gid] = sent
            return {"ok": True, "posted": False, "why": "rate"}
        me = guild.me
        perms = channel.permissions_for(me) if me else None
        if not perms or not (perms.send_messages and perms.embed_links):
            return {"ok": True, "posted": False, "why": "no_perms"}
        try:
            await channel.send(embed=self.highlight_embed(kind, body), view=play_view(self.app_id),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.info("Strip feed post failed in %s: %s", gid, e)
            return {"ok": True, "posted": False, "why": "discord"}
        sent.append(now)
        self._sent[gid] = sent
        return {"ok": True, "posted": True}

    def highlight_embed(self, kind: str, body: dict) -> discord.Embed:
        title, text = KINDS[kind]
        player = body.get("player") if isinstance(body.get("player"), dict) else {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        clip = lambda v, n=40: discord.utils.escape_markdown(str(v or "")[:n]) or "someone"
        fields = {"name": clip(player.get("name")), "game": clip(data.get("game")), "score": clip(data.get("score")),
                  "partner": clip(data.get("partner")), "title": clip(data.get("title")), "job": clip(data.get("job")),
                  "level": clip(data.get("level"), 4)}
        if kind == "jackpot":
            net = data.get("net")
            amount = f"{int(net):,}" if isinstance(net, (int, float)) else "a pile of"
            jp = data.get("jackpot")
            fields["won"] = (f"hit the **{clip(jp)}** jackpot for **{amount}** chips!" if jp
                             else f"just won **{amount}** chips at {clip(data.get('game'))}!")
        e = embed(title, text.format(**fields), GOLD if kind in ("record", "jackpot", "cup") else BRAND)
        online = self.online
        e.set_footer(text=f"THE STRIP · {online:,} playing now · one world for all of Discord" if online else "THE STRIP · one world for all of Discord")
        return e

    async def _update_presence(self) -> None:
        prefix = self.bot.settings.default_prefix
        text = f"THE STRIP · {self.online:,} playing | {prefix}thestrip" if self.online else None
        last, when = self._presence
        if text == last or time.time() - when < 120:
            return
        self._presence = (text, time.time())
        try:
            if text:
                await self.bot.change_presence(activity=discord.Game(name=text))
            else:
                await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"you 👁️ | {prefix}help"))
        except Exception:
            log.debug("presence update failed", exc_info=True)

    # ---------------------------------------------------------------- commands
    def play_embed(self, guild: discord.Guild | None) -> discord.Embed:
        n = self.online
        e = embed("🎰 THE STRIP",
                  "A pixel Las Vegas you play right here in Discord, one world shared by every server.\n"
                  "Work real shifts, hit the casino, set arcade records, buy a home, date and get married. "
                  "Your SYKE money is your Strip money.\n\n"
                  + (f"**{n:,}** playing right now." if n else "Be the first one on tonight.")
                  + ("\n\nTap **Play** below, or start it from the 🚀 Activities button in any voice channel or DM." if launch_url(self.app_id)
                     else "\n\nStart it from the 🚀 Activities button in any voice channel or DM."), GOLD)
        return e

    @commands.hybrid_command(name="thestrip", with_app_command=False, aliases=["playstrip", "stripplay"],
                             description="Invite everyone to THE STRIP, with a Play button")
    @commands.guild_only()
    async def thestrip(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self.play_embed(ctx.guild), view=play_view(self.app_id), mention_author=False)

    @commands.hybrid_command(name="stripfeed", with_app_command=False, description="Post Strip highlights in a channel (or turn them off)")
    @commands.guild_only()
    async def stripfeed(self, ctx: commands.Context, channel: discord.TextChannel | None = None, setting: str | None = None) -> None:
        from .bot import is_admin
        if not is_admin(self.bot, ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        await self.set_feed(ctx, channel, setting)

    async def set_feed(self, ctx, channel: discord.TextChannel | None, setting: str | None) -> None:
        p = self.bot.display_prefix(ctx.guild.id)
        if setting and setting.lower() in ("off", "stop", "disable"):
            self.bot.storage.update_config(ctx.guild.id, FEED_KEY, channel=None)
            await ctx.reply(embed=embed("📰 Strip feed off", f"No more Strip highlights here. Turn them back on with `{p}stripfeed #channel`.", OK), mention_author=False)
            return
        if channel is None:
            cfg = self.bot.storage.get_config(ctx.guild.id, FEED_KEY)
            cur = ctx.guild.get_channel(int(cfg.get("channel") or 0)) if cfg.get("channel") else None
            await ctx.reply(embed=embed("📰 Strip feed",
                                        (f"Highlights go to {cur.mention}." if cur else "Highlights are off.")
                                        + f"\n\n`{p}stripfeed #channel` posts new arcade records, big casino wins, weddings, weekly cup "
                                        "champions, promotions and new arrivals from **this server's** players, each with a Play button "
                                        f"(at most one every {GAP} s, {PER_DAY} a day). `{p}stripfeed off` stops it.", BRAND), mention_author=False)
            return
        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.reply(embed=embed("📰 Can't post there", f"I need **Send Messages** and **Embed Links** in {channel.mention}.", WARN), mention_author=False)
            return
        self.bot.storage.update_config(ctx.guild.id, FEED_KEY, channel=channel.id)
        await ctx.reply(embed=embed("📰 Strip feed on", f"Strip highlights from this server's players will show up in {channel.mention}.", OK), mention_author=False)
        try:
            await channel.send(embed=self.play_embed(ctx.guild), view=play_view(self.app_id))
        except discord.HTTPException:
            pass
