"""THE STRIP's feed: highlights from the game posted in each server, a Play button, and the live player count.

The game server POSTs to /strip/api/feed (same secret as the rest of the Strip API):
  {"kind": "online", "online": 42, "app_id": "..."}                    -> SYKE's status shows the count
  {"kind": "record"|"jackpot"|"wedding"|"cup"|"promotion"|"newplayer",
   "guild_id": "<the server the player launched from>", "player": {"id", "name"}, "data": {...}, "online": n}
Highlights are only posted where a server's admins chose a channel (`!stripfeed #channel` or `/strip feed`),
at most one every 90 seconds and 25 a day per server. Nothing is stored about the players.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, OK, WARN, embed

log = logging.getLogger("syke.strip_feed")
FEED_KEY = "strip_feed"
GAP = 90            # seconds between posts in one server
PER_DAY = 25        # posts per server per UTC day
GOLD = discord.Colour.from_str("#F5C542")
PINK = discord.Colour.from_str("#F04491")
PHOTO_GAP = 60          # THE STRIP 3.21 outfit photos: their own pace, apart from highlights
PHOTO_PER_DAY = 40
PHOTO_MAX_BYTES = 600_000
KINDS = {
    "record": ("🥇 New arcade record", "**{name}** just set the all-time record on **{game}** at Pixel Palace: **{score}**. Think you can beat it?"),
    "jackpot": ("🎰 Big win at the casino", "**{name}** {won} Who's next?"),
    "wedding": ("💒 Wedding bells on the Strip", "**{name}** and **{partner}** just got married at the chapel. Congratulations!"),
    "cup": ("🏆 Weekly cup champion", "**{name}** won this week's **{game}** cup at Pixel Palace."),
    "promotion": ("💼 Promotion", "**{name}** is now **{title}** ({job}, level {level})."),
    "newplayer": ("🌆 New arrival", "**{name}** from this server just moved to THE STRIP. Come say hi!"),
    "live": ("🔴 Live on THE STRIP", "**{name}** is playing THE STRIP {where}. Hop in and play together!"),
    "pet": ("🐾 New best friend", "**{name}** just adopted **{pet}**, a {breed}, on THE STRIP. Come say hi (pats welcome)!"),
}


def launch_url(app_id: str) -> str | None:
    return f"https://discord.com/activities/{app_id}" if app_id and app_id.isdigit() else None


# the showcase: real screenshots from the game, one page per feature (syke/assets/strip/*.jpg)
ASSETS = Path(__file__).parent / "assets" / "strip"
SHOWCASE = [
    ("cover", "🎰 THE STRIP", "A pixel Las Vegas you play **right here in Discord**, in one world shared by every server that has SYKE.\n\n"
     "Work real shifts, hit the casino, set arcade records, buy a home, make friends, date, start a club, and get married. "
     "**Your SYKE money is your Strip money.**"),
    ("strip", "🌃 One Strip for all of Discord", "Neon storefronts, traffic, weather and a day–night cycle. Players from every server walk the same streets. "
     "Chat with whoever's nearby, emote, drive your own car, and tap anyone to see their profile."),
    ("casino", "🃏 The casino", "Blackjack, roulette, slots and Hi-Lo with your SYKE chips, plus a high-limit room, a bar and a daily payout at the Players Club. "
     "Big wins get announced to the whole Strip."),
    ("arcade", "🕹️ Pixel Palace", "Eight real arcade games with high-score boards, claw machines full of prizes, head-to-head matches, "
     "a ghost of the record lap in Neon Rally, and a weekly cup with trophies."),
    ("downtown", "💼 A life to live", "Clock in at any storefront and do the actual job. Careers level up with titles and uniforms. "
     "Rent or buy a home and decorate it, own a storefront that earns every hour, and catch a community event every two hours."),
    ("phone", "📱 Your phone", "Messages and calls, friends, groups for tonight and permanent clubs, StripSocial posts and photos, "
     "a player market, maps with directions, a calendar and your profile, all in your pocket."),
    ("profile", "💕 People", "Make friends, go on dates, propose and get married at the chapel, then move in together. "
     "Profiles show off your trophies, homes and cars, with privacy you control."),
    ("pets", "🐾 Pets", "Adopt a dog or a cat: pick a breed at Paws & Claws or give a rescue a home at the shelter (new animals every day). "
     "They follow you around town, nap when you stop, and other players can stop for a pat. Feed them, walk them, dress them in tiny cowboy hats."),
]


def showcase_file(key: str) -> discord.File | None:
    path = ASSETS / f"{key}.jpg"
    return discord.File(str(path), filename="strip.jpg") if path.exists() else None


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
        # the Activity's id (for the Play link): an env override, else what the game last told us (kept across restarts)
        saved = self.bot.storage.get_config(0, FEED_KEY) if getattr(self.bot, "storage", None) else {}
        self.app_id = str(os.environ.get("STRIP_ACTIVITY_ID") or saved.get("app_id") or "")
        self._presence = (None, 0.0)       # (text, when)
        self._sent: dict[int, list[float]] = {}

    # ---------------------------------------------------------------- from the game server
    async def handle(self, body: dict) -> dict:
        app_id = str(body.get("app_id") or "")
        if app_id.isdigit() and app_id != self.app_id and not os.environ.get("STRIP_ACTIVITY_ID"):
            self.app_id = app_id
            try:
                self.bot.storage.update_config(0, FEED_KEY, app_id=app_id)
            except Exception:
                log.debug("could not keep the activity id", exc_info=True)
        try:
            self.online = max(0, min(100000, int(body.get("online", self.online) or 0)))
        except (TypeError, ValueError):
            pass
        await self._update_presence()
        kind = str(body.get("kind") or "")
        if kind == "online":
            return {"ok": True}
        if kind == "photo":
            return await self.photo(body)
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
        if kind == "live":   # 3.25 name the voice channel it's running in (a clickable link), and how many are in the call
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            where = "right now"
            try:
                vc = guild.get_channel(int(data.get("channel") or 0))
            except (TypeError, ValueError):
                vc = None
            if isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
                n = len(vc.members)
                where = f"in {vc.mention}" + (f" ({n} in the call)" if n > 1 else "")
            body = {**body, "data": {**data, "where": where}}
        try:
            await channel.send(embed=self.highlight_embed(kind, body), view=play_view(self.app_id),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.info("Strip feed post failed in %s: %s", gid, e)
            return {"ok": True, "posted": False, "why": "discord"}
        sent.append(now)
        self._sent[gid] = sent
        return {"ok": True, "posted": True}

    async def photo(self, body: dict) -> dict:
        """An outfit photo from the game's photo studio: a PNG the game server drew itself from the player's saved look
        (never an upload), posted in the player's own server where its admins chose a channel and didn't mute photos."""
        try:
            gid = int(body.get("guild_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_ids"}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        try:
            png = base64.b64decode(str(data.get("image") or ""), validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "bad_image"}
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > PHOTO_MAX_BYTES:
            return {"ok": False, "error": "bad_image"}
        guild = self.bot.get_guild(gid) if gid else None
        if guild is None:
            return {"ok": True, "posted": False, "why": "not_here"}
        cfg = self.bot.storage.get_config(gid, FEED_KEY)
        channel = guild.get_channel(int(cfg.get("channel") or 0)) if cfg.get("channel") else None
        if channel is None or cfg.get("off"):
            return {"ok": True, "posted": False, "why": "no_channel"}
        if "photo" in set(cfg.get("mute") or []):
            return {"ok": True, "posted": False, "why": "muted_kind"}
        now, key = time.time(), -gid
        sent = [t for t in self._sent.get(key, []) if now - t < 86400]
        if (sent and now - sent[-1] < PHOTO_GAP) or len(sent) >= PHOTO_PER_DAY:
            self._sent[key] = sent
            return {"ok": True, "posted": False, "why": "rate", "retry": int(PHOTO_GAP - (now - sent[-1])) if sent else 0}
        me = guild.me
        perms = channel.permissions_for(me) if me else None
        if not perms or not (perms.send_messages and perms.embed_links and perms.attach_files):
            return {"ok": True, "posted": False, "why": "no_perms"}
        player = body.get("player") if isinstance(body.get("player"), dict) else {}
        clip = lambda v, n=40: discord.utils.escape_markdown(str(v or "")[:n])
        outfit = [clip(x, 32) for x in (data.get("outfit") or [])[:8] if str(x or "").strip()]
        others = [clip(x, 32) for x in (data.get("with") or [])[:3] if str(x or "").strip()]
        who = clip(player.get('name')) or 'Someone'
        lead = f"**{who}** and **{'**, **'.join(others)}** stepped into the photo studio." if others else f"**{who}** stepped into the photo studio."
        caption = clip(data.get("caption"), 120).strip()
        e = embed("📸 Group shot on the Strip" if others else "📸 Fit check on the Strip", lead + (f"\n> {caption}" if caption else ""), PINK)
        if outfit:
            e.add_field(name="Wearing", value=" · ".join(outfit), inline=False)
        e.set_image(url="attachment://fit.png")
        online = self.online
        e.set_footer(text=f"THE STRIP · {online:,} playing now" if online else "THE STRIP · one world for all of Discord")
        try:
            await channel.send(embed=e, file=discord.File(io.BytesIO(png), filename="fit.png"), view=play_view(self.app_id),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as ex:
            log.info("Strip photo post failed in %s: %s", gid, ex)
            return {"ok": True, "posted": False, "why": "discord"}
        sent.append(now)
        self._sent[key] = sent
        return {"ok": True, "posted": True, "channel": channel.name}

    def highlight_embed(self, kind: str, body: dict) -> discord.Embed:
        title, text = KINDS[kind]
        player = body.get("player") if isinstance(body.get("player"), dict) else {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        clip = lambda v, n=40: discord.utils.escape_markdown(str(v or "")[:n]) or "someone"
        fields = {"name": clip(player.get("name")), "game": clip(data.get("game")), "score": clip(data.get("score")),
                  "partner": clip(data.get("partner")), "title": clip(data.get("title")), "job": clip(data.get("job")),
                  "level": clip(data.get("level"), 4), "pet": clip(data.get("pet"), 20), "breed": clip(data.get("breed"), 30),
                  "where": str(data.get("where") or "right now")[:80]}
        if kind == "jackpot":
            net = data.get("net")
            amount = f"{int(net):,}" if isinstance(net, (int, float)) else "a pile of"
            jp = data.get("jackpot")
            fields["won"] = (f"hit the **{clip(jp)}** jackpot for **{amount}** chips!" if jp
                             else f"just won **{amount}** chips at {clip(data.get('game'))}!")
        e = embed(title, text.format(**fields), GOLD if kind in ("record", "jackpot", "cup") else PINK if kind == "live" else BRAND)
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
    def play_embed(self, guild: discord.Guild | None, page: int = 0) -> discord.Embed:
        """One page of the showcase: a screenshot, what it is, how many are on, and how to play."""
        key, title, text = SHOWCASE[page % len(SHOWCASE)]
        n, url = self.online, launch_url(self.app_id)
        e = embed(title, text, GOLD)
        if page == 0:
            e.add_field(name="🟢 Right now", value=f"**{n:,}** playing" if n else "Be the first one on tonight", inline=True)
            e.add_field(name="💸 Money", value="Your SYKE wallet", inline=True)
            e.add_field(name="🎁 New players", value="A tutorial and a welcome bonus", inline=True)
            e.add_field(name="▶️ How to play", value=(f"Tap **Play THE STRIP** below, or open the 🚀 **Activities** button in any voice channel or DM."
                                                       if url else "Open the 🚀 **Activities** button in any voice channel or DM and pick THE STRIP."), inline=False)
        if (ASSETS / f"{key}.jpg").exists():
            e.set_image(url="attachment://strip.jpg")
        e.set_footer(text=f"Page {page % len(SHOWCASE) + 1}/{len(SHOWCASE)} · " + (f"{n:,} on the Strip now" if n else "One world for all of Discord"))
        return e

    def showcase_view(self, page: int = 0, owner: int | None = None) -> discord.ui.View:
        return ShowcaseView(self, page, owner)

    async def showcase(self, ctx: commands.Context) -> None:
        f = showcase_file(SHOWCASE[0][0])
        view = self.showcase_view(0, getattr(ctx.author, "id", None))
        msg = await ctx.reply(embed=self.play_embed(ctx.guild, 0), view=view, mention_author=False, **({"file": f} if f else {}))
        view.message = msg

    @commands.hybrid_command(name="thestrip", aliases=["playstrip", "stripplay", "game", "strip"], with_app_command=False,
                             description="Show off THE STRIP game, with screenshots and a Play button")
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.channel)
    async def play(self, ctx: commands.Context) -> None:
        await self.showcase(ctx)

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
                                        f"(at most one every {GAP} s, {PER_DAY} a day). `{p}stripfeed off` stops it.\n\n"
                                        f"📸 Outfit photos from the game's photo studio go to the same channel. `{p}stripphotos off` turns just those off.\n"
                                        f"🔴 When someone starts playing in one of your voice channels, a \"come join\" post links that channel (at most every 3 hours). `{p}striplive off` turns just those off.", BRAND), mention_author=False)
            return
        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.reply(embed=embed("📰 Can't post there", f"I need **Send Messages** and **Embed Links** in {channel.mention}.", WARN), mention_author=False)
            return
        self.bot.storage.update_config(ctx.guild.id, FEED_KEY, channel=channel.id)
        await ctx.reply(embed=embed("📰 Strip feed on", f"Strip highlights from this server's players will show up in {channel.mention}.", OK), mention_author=False)
        try:
            f = showcase_file("cover")
            await channel.send(embed=self.play_embed(ctx.guild), view=play_view(self.app_id), **({"file": f} if f else {}))
        except discord.HTTPException:
            pass


    @commands.hybrid_command(name="stripphotos", with_app_command=False, description="Turn outfit photos from THE STRIP on or off in the feed channel")
    @commands.guild_only()
    async def stripphotos(self, ctx: commands.Context, setting: str | None = None) -> None:
        from .bot import is_admin
        if not is_admin(self.bot, ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        cfg = self.bot.storage.get_config(ctx.guild.id, FEED_KEY)
        mute = set(cfg.get("mute") or [])
        if setting and setting.lower() in ("off", "stop", "disable", "no"):
            mute.add("photo")
        elif setting and setting.lower() in ("on", "start", "enable", "yes"):
            mute.discard("photo")
        else:
            await ctx.reply(embed=embed("📸 Strip photos", ("Off" if "photo" in mute else "On") + " in this server's Strip feed channel. "
                                        f"`{self.bot.display_prefix(ctx.guild.id)}stripphotos on|off`", BRAND), mention_author=False)
            return
        self.bot.storage.update_config(ctx.guild.id, FEED_KEY, mute=sorted(mute))
        await ctx.reply(embed=embed("📸 Strip photos " + ("off" if "photo" in mute else "on"),
                                    "Outfit photos won't be posted here." if "photo" in mute else "Outfit photos from this server's players will be posted in the feed channel.", OK), mention_author=False)

    @commands.hybrid_command(name="striplive", with_app_command=False, description="Turn THE STRIP's 'come join' posts on or off in the feed channel")
    @commands.guild_only()
    async def striplive(self, ctx: commands.Context, setting: str | None = None) -> None:
        """3.25: "Ava is playing THE STRIP in 🔊 Lounge" posts, at most one every 3 hours per server."""
        from .bot import is_admin
        if not is_admin(self.bot, ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        cfg = self.bot.storage.get_config(ctx.guild.id, FEED_KEY)
        mute = set(cfg.get("mute") or [])
        if setting and setting.lower() in ("off", "stop", "disable", "no"):
            mute.add("live")
        elif setting and setting.lower() in ("on", "start", "enable", "yes"):
            mute.discard("live")
        else:
            await ctx.reply(embed=embed("🔴 Strip live posts", ("Off" if "live" in mute else "On") + " in this server's Strip feed channel: when someone starts "
                                        "playing in a voice channel here, a post links that channel so others can hop in (at most every 3 hours). "
                                        f"`{self.bot.display_prefix(ctx.guild.id)}striplive on|off`", BRAND), mention_author=False)
            return
        self.bot.storage.update_config(ctx.guild.id, FEED_KEY, mute=sorted(mute))
        await ctx.reply(embed=embed("🔴 Strip live posts " + ("off" if "live" in mute else "on"),
                                    "No more \"come join\" posts here." if "live" in mute else "When someone starts playing in a voice channel here, I'll post a link to it in the feed channel.", OK), mention_author=False)


class ShowcaseView(discord.ui.View):
    """Page through the showcase (anyone can), with the Play link always there."""

    def __init__(self, feed: StripFeed, page: int = 0, owner: int | None = None) -> None:
        super().__init__(timeout=900)
        self.feed, self.page, self.message = feed, page, None
        url = launch_url(feed.app_id)
        if url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Play THE STRIP", emoji="🎰", url=url, row=0))

    async def _show(self, interaction: discord.Interaction, page: int) -> None:
        self.page = page % len(SHOWCASE)
        f = showcase_file(SHOWCASE[self.page][0])
        await interaction.response.edit_message(embed=self.feed.play_embed(interaction.guild, self.page), view=self,
                                                attachments=[f] if f else [])

    @discord.ui.button(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self.page - 1)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.primary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self.page + 1)

    async def on_timeout(self) -> None:   # keep the Play link, drop the page buttons
        if self.message is None:
            return
        for item in list(self.children):
            if not isinstance(item, discord.ui.Button) or item.style != discord.ButtonStyle.link:
                self.remove_item(item)
        try:
            await self.message.edit(view=self if self.children else None)
        except discord.HTTPException:
            pass
