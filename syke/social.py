"""Social commands: AFK, birthdays, Wrapped, vibe check and catch-up.

* `afk [reason]`: anyone who pings you while you're away hears why; when you're back SYKE lists who
  pinged you, with links. Your nickname gets "[AFK]" while you're gone if SYKE can change it.
* `birthday set 24.12`: SYKE wishes you a happy birthday in the birthday channel on the day (server
  timezone), gives you the birthday role for 24 hours, and a cash gift if the Strip is open.
* `wrapped [@user]`: a year-in-review card, Spotify Wrapped style, from the messages SYKE has stored.
* `vibe`: reads the last 100 messages here and sums up the mood.
* `catchup`: what happened in this channel since you last spoke, plus who mentioned you.

Wrapped and catch-up only use what SYKE stores from watched channels, and skip members who opted out.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import io
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import ai
from .lexicon import STOPWORDS
from .moderation import BRAND, OK, WARN, embed, moderator, problem
from .tldr import (Line, digest, from_discord_message, offline_summary, prompt_facts, prompt_sample, span_label,
                   vibe_line)

log = logging.getLogger("syke.social")

AFK_KEY = "afk"
AFK_NOTICE_GAP = 60
AFK_PINGS_KEPT = 25
AFK_TAG = "[AFK] "
BDAY_KEY = "birthdays"
BDAY_GIFT = 1_000
BDAY_ROLE_NAME = "🎂 Birthday"
BDAY_COLOUR = "#F472B6"
VIBE_MESSAGES = 100
CATCHUP_FETCH = 900
CATCHUP_MIN = 5
FONTS = Path(__file__).parent / "assets" / "fonts"
WORD = re.compile(r"[a-zA-ZäöüÄÖÜß']{3,}")
EXTRA_SKIP = {"https", "http", "www", "com", "the", "and", "you", "that", "this", "but", "for", "with", "just",
              "not", "are", "was", "have", "its", "it's", "i'm", "dont", "don't", "like", "what", "yeah", "can",
              "get", "got", "all", "they", "she", "his", "her", "him", "about", "your", "one", "out", "how",
              "der", "die", "das", "und", "ist", "ich", "nicht", "mit", "was", "auf", "ein", "eine", "aber", "auch"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# Birthdays

def parse_birthday(text: str) -> tuple[int, int] | None:
    """(month, day) from "24.12", "24/12", "12-24", "24 dec", "december 24"."""
    text = (text or "").strip().lower()
    months = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
    months.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./]\d{2,4})?", text)
    day = month = None
    if m:
        day, month = int(m.group(1)), int(m.group(2))
    elif re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        _, month, day = (int(x) for x in text.split("-"))
    else:
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?|([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?", text)
        if m:
            day = int(m.group(1) or m.group(4))
            month = months.get(m.group(2) or m.group(3))
    if not month or not day or not 1 <= month <= 12:
        return None
    if day > calendar.monthrange(2024, month)[1]:  # 2024 so 29 Feb is allowed
        return None
    return month, day


def birthday_text(month: int, day: int) -> str:
    return f"{day} {calendar.month_name[month]}"


def is_birthday(month: int, day: int, today) -> bool:
    if (today.month, today.day) == (month, day):
        return True
    leap = calendar.isleap(today.year)
    return (month, day) == (2, 29) and not leap and (today.month, today.day) == (2, 28)


def days_until(month: int, day: int, today) -> int:
    for year in (today.year, today.year + 1):
        try:
            target = today.replace(year=year, month=month, day=day)
        except ValueError:
            target = today.replace(year=year, month=2, day=28)
        if target >= today:
            return (target - today).days
    return 365


# Wrapped

def wrapped_stats(rows: list[tuple], tz, server_rank: tuple[int, int] | None = None,
                  partners: list[tuple[str, int]] | None = None, channel_names: dict[int, str] | None = None) -> dict:
    """Everything on the Wrapped card, from (message_id, channel_id, content, created_at, laughs, reactions)."""
    words, emojis, hours, weekdays, channels, days = Counter(), Counter(), Counter(), Counter(), Counter(), set()
    best = None
    for _, channel_id, content, created, laughs, _total in rows:
        moment = datetime.fromtimestamp(created, tz)
        hours[moment.hour] += 1
        weekdays[moment.weekday()] += 1
        days.add(moment.date())
        channels[channel_id] += 1
        for w in WORD.findall(content or ""):
            lw = w.lower().strip("'")
            if lw not in STOPWORDS and lw not in EXTRA_SKIP and not lw.startswith("http"):
                words[lw] += 1
        for found in re.findall(r"<a?:\w+:\d+>", content or ""):
            emojis[found] += 1
        for found in emoji_lib.emoji_list(content or ""):
            emojis[found["emoji"]] += 1
        if laughs and (best is None or laughs > best[0]) and len((content or "").split()) >= 2:
            best = (laughs, content)
    streak = longest = 0
    previous = None
    for day in sorted(days):
        streak = streak + 1 if previous is not None and (day - previous).days == 1 else 1
        longest = max(longest, streak)
        previous = day
    hour = hours.most_common(1)[0][0] if hours else None
    names = channel_names or {}
    return {
        "messages": len(rows),
        "words": sum(len((r[2] or "").split()) for r in rows),
        "top_words": [w for w, _ in words.most_common(5)],
        "emoji": emojis.most_common(1)[0][0] if emojis else None,
        "emoji_count": emojis.most_common(1)[0][1] if emojis else 0,
        "hour": hour,
        "weekday": DAYS[weekdays.most_common(1)[0][0]] if weekdays else None,
        "channel": names.get(channels.most_common(1)[0][0]) if channels else None,
        "active_days": len(days),
        "streak": longest,
        "best": best[1] if best else None,
        "best_laughs": best[0] if best else 0,
        "rank": server_rank,
        "partners": partners or [],
    }


def talk_partners(activity: list[tuple], user_id: int, window: float = 120.0) -> list[tuple[str, int]]:
    """Who someone talks with most: other people posting within two minutes of them, same channel."""
    last: dict[int, tuple[int, str, float]] = {}
    mine: dict[int, float] = {}
    scores: Counter = Counter()
    names: dict[int, str] = {}
    for channel_id, author_id, author_name, created in activity:
        if author_id == user_id:
            mine[channel_id] = created
            prev = last.get(channel_id)
            if prev and prev[0] != user_id and created - prev[2] <= window:
                scores[prev[0]] += 1
        else:
            names[author_id] = author_name or names.get(author_id, "")
            seen = mine.get(channel_id)
            if seen is not None and created - seen <= window:
                scores[author_id] += 1
        last[channel_id] = (author_id, author_name, created)
    return [(names.get(uid) or "someone", n) for uid, n in scores.most_common(3) if names.get(uid)]


def hour_label(hour: int | None) -> str:
    if hour is None:
        return "—"
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12}{suffix}"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.truetype(str(FONTS / "Poppins-Bold.ttf"), size)


def _fit(draw, text: str, font_name: str, size: int, width: int, minimum: int = 18):
    while size > minimum and draw.textlength(text, font=_font(font_name, size)) > width:
        size -= 2
    return _font(font_name, size)


def render_wrapped(name: str, year: int, stats: dict, avatar: bytes | None = None,
                   emoji_image: Image.Image | None = None, server: str = "") -> bytes:
    W, H = 1080, 1350
    card = Image.new("RGB", (W, H), (20, 12, 36))
    top, bottom = (124, 58, 237), (236, 72, 153)
    grad = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / H
        grad.putpixel((0, y), tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    card.paste(grad.resize((W, H)), (0, 0))
    shade = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shade).ellipse((-300, 700, 900, 1700), fill=70)
    card.paste((10, 6, 20), (0, 0), shade)
    draw = ImageDraw.Draw(card)
    white, soft = (255, 255, 255), (255, 255, 255, 200)
    if avatar:
        with contextlib.suppress(OSError):
            face = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"), (150, 150), Image.LANCZOS)
            mask = Image.new("L", (150, 150), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 150, 150), fill=255)
            card.paste(face, (70, 70), mask)
    draw.text((245, 82), f"{year} WRAPPED", font=_font("Poppins-Bold.ttf", 34), fill=(255, 230, 250))
    draw.text((245, 122), name, font=_fit(draw, name, "Poppins-Bold.ttf", 64, 770), fill=white)
    if server:
        draw.text((247, 200), server, font=_fit(draw, server, "Poppins-Medium.ttf", 26, 760), fill=(240, 220, 255))

    def block(x, y, label, value, size=78, width=440):
        draw.text((x, y), label.upper(), font=_font("Poppins-Medium.ttf", 26), fill=(255, 225, 245))
        draw.text((x, y + 36), value, font=_fit(draw, value, "Poppins-Bold.ttf", size, width, 30), fill=white)

    block(70, 300, "Messages sent", f"{stats['messages']:,}", 96)
    block(560, 300, "Words typed", f"{stats['words']:,}", 96)
    block(70, 470, "Busiest hour", hour_label(stats.get("hour")), 72)
    block(560, 470, "Favourite day", stats.get("weekday") or "—", 64)
    block(70, 630, "Days active", f"{stats['active_days']}  ·  best streak {stats['streak']}", 48, 940)
    y = 770
    draw.text((70, y), "TOP WORDS", font=_font("Poppins-Medium.ttf", 26), fill=(255, 225, 245))
    for i, word in enumerate(stats.get("top_words")[:5] or ["—"]):
        draw.text((70, y + 40 + i * 52), f"{i + 1}  {word}", font=_fit(draw, f"{i + 1}  {word}", "Poppins-Bold.ttf",
                                                                       42, 440, 24), fill=white)
    draw.text((560, y), "FAVOURITE EMOJI", font=_font("Poppins-Medium.ttf", 26), fill=(255, 225, 245))
    if emoji_image is not None:
        icon = emoji_image.convert("RGBA").resize((120, 120), Image.LANCZOS)
        card.paste(icon, (560, y + 44), icon)
        draw.text((700, y + 80), f"× {stats['emoji_count']:,}", font=_font("Poppins-Bold.ttf", 44), fill=white)
    elif stats.get("emoji"):
        label = re.sub(r"<a?:(\w+):\d+>", r":\1:", stats["emoji"])
        draw.text((560, y + 50), f"{label}  × {stats['emoji_count']:,}"[:28], font=_font("Poppins-Bold.ttf", 40),
                  fill=white)
    else:
        draw.text((560, y + 50), "none", font=_font("Poppins-Bold.ttf", 40), fill=white)
    draw.text((560, y + 190), "TALKS TO MOST", font=_font("Poppins-Medium.ttf", 26), fill=(255, 225, 245))
    partners = [n for n, _ in stats.get("partners") or []][:3] or ["the void"]
    for i, partner in enumerate(partners):
        draw.text((560, y + 230 + i * 48), partner, font=_fit(draw, partner, "Poppins-Bold.ttf", 38, 450, 22),
                  fill=white)
    foot = []
    if stats.get("rank"):
        place, total = stats["rank"]
        pct = max(1, math.ceil(place / max(total, 1) * 100))
        foot.append(f"#{place} of {total} talkers (top {pct}%)")
    if stats.get("channel"):
        foot.append(f"lives in #{stats['channel']}")
    footer = "  ·  ".join(foot)
    if footer:
        draw.text((70, 1250), footer, font=_fit(draw, footer, "Poppins-Medium.ttf", 30, 940, 18), fill=white)
    draw.text((W - 150, 1290), "SYKE", font=_font("Poppins-Bold.ttf", 28), fill=(255, 220, 245))
    out = io.BytesIO()
    card.save(out, "PNG", optimize=True)
    return out.getvalue()


# Vibe

VIBE_PROMPT = """You read the last messages of a Discord channel and judge its vibe like a funny friend would.
Score 0-100 for each of: hype, chaos, wholesome, toxic, drama, brainrot. Give the mood in 1-3 words, one \
fitting emoji, and a one or two sentence read on what's going on, naming people as plain text. Be specific \
and funny, never cruel. No @mentions, no slurs.
Reply with JSON only: {"mood": str, "emoji": str, "read": str, "scores": {"hype": int, "chaos": int, \
"wholesome": int, "toxic": int, "drama": int, "brainrot": int}}"""
VIBE_KEYS = ("hype", "chaos", "wholesome", "toxic", "drama", "brainrot")
TOXIC_WORDS = re.compile(r"\b(stfu|shut up|idiot|stupid|hate|trash|kys|loser|dumb|fuck you|fuck off)\b", re.I)
WHOLESOME_WORDS = re.compile(r"\b(love|thank|thanks|congrats|proud|miss you|ily|❤️|🥰|💖)\b|❤|🥰|💖", re.I)
LAUGH_WORDS = re.compile(r"\b(lol+|lmf?ao+|haha+|dead|crying)\b|😂|🤣|💀|😭", re.I)


def offline_vibe(lines: list[Line]) -> dict:
    texts = [line.text for line in lines]
    n = max(1, len(texts))
    caps = sum(1 for t in texts if len(t) > 5 and t.upper() == t and any(c.isalpha() for c in t)) / n
    laughs = sum(1 for t in texts if LAUGH_WORDS.search(t)) / n
    toxic = sum(1 for t in texts if TOXIC_WORDS.search(t)) / n
    kind = sum(1 for t in texts if WHOLESOME_WORDS.search(t)) / n
    questions = sum(1 for t in texts if "?" in t) / n
    spread = len({line.author for line in lines})
    pace = 0.0
    if len(lines) > 1 and lines[-1].created and lines[0].created:
        pace = len(lines) / max(60.0, lines[-1].created - lines[0].created) * 60  # messages a minute
    scores = {
        "hype": min(100, int(pace * 12 + laughs * 120)),
        "chaos": min(100, int(caps * 220 + spread * 4 + pace * 6)),
        "wholesome": min(100, int(kind * 300)),
        "toxic": min(100, int(toxic * 320)),
        "drama": min(100, int(toxic * 150 + questions * 80)),
        "brainrot": min(100, int(laughs * 140 + caps * 100)),
    }
    top = max(scores, key=scores.get)
    mood = {"hype": "Hype", "chaos": "Pure chaos", "wholesome": "Wholesome", "toxic": "Hostile",
            "drama": "Drama", "brainrot": "Brainrot"}[top] if scores[top] >= 15 else "Chill"
    icon = {"Hype": "🔥", "Pure chaos": "🌪️", "Wholesome": "🥰", "Hostile": "😤", "Drama": "🍿", "Brainrot": "🧠",
            "Chill": "😌"}[mood]
    return {"mood": mood, "emoji": icon, "read": vibe_line(lines), "scores": scores}


def bar(value: int, width: int = 12) -> str:
    filled = round(max(0, min(100, value)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


class Social(commands.Cog, name="Social"):
    recap = app_commands.Group(name="recap", description="Wrapped, vibe check and catch-up", guild_only=True)

    def __init__(self, bot) -> None:
        self.bot = bot
        self._afk_noticed: dict[tuple[int, int], float] = {}
        self._emoji_cache: dict[str, Image.Image | None] = {}
        self._http = None

    async def cog_load(self) -> None:
        self.birthday_loop.start()

    async def cog_unload(self) -> None:
        self.birthday_loop.cancel()
        if self._http is not None:
            await self._http.close()

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    async def fetch(self, url: str) -> bytes | None:
        import aiohttp
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        try:
            async with self._http.get(url) as resp:
                return await resp.read() if resp.status == 200 else None
        except Exception:
            return None

    # AFK

    def afk_data(self, guild_id: int) -> dict:
        return dict(self.bot.storage.get_config(guild_id, AFK_KEY))

    @commands.hybrid_command(name="afk", aliases=["away", "brb"], description="Go AFK: anyone who pings you hears why")
    @app_commands.describe(reason="Why you're away")
    @app_commands.guild_only()
    @commands.guild_only()
    async def afk(self, ctx: commands.Context, *, reason: str = "") -> None:
        reason = discord.utils.escape_mentions(reason.strip())[:180] or "AFK"
        data = self.afk_data(ctx.guild.id)
        entry = {"reason": reason, "since": time.time(), "pings": [], "nick": None}
        member = ctx.author
        me = ctx.guild.me
        if me is not None and me.guild_permissions.manage_nicknames and member.top_role < me.top_role \
                and member.id != ctx.guild.owner_id and not (member.display_name or "").startswith(AFK_TAG):
            old = member.nick
            with contextlib.suppress(discord.HTTPException):
                await member.edit(nick=(AFK_TAG + member.display_name)[:32], reason="SYKE: AFK")
                entry["nick"] = old or ""
        data[str(member.id)] = entry
        self.bot.storage.set_config(ctx.guild.id, AFK_KEY, data)
        await ctx.reply(embed=embed("💤 You're AFK", f"**{reason}**\nAnyone who pings you will be told. Send any "
                                                    f"message to come back.", BRAND))

    async def _back(self, message: discord.Message, entry: dict, data: dict) -> None:
        guild, member = message.guild, message.author
        data.pop(str(member.id), None)
        self.bot.storage.set_config(guild.id, AFK_KEY, data)
        if entry.get("nick") is not None and (member.display_name or "").startswith(AFK_TAG):
            with contextlib.suppress(discord.HTTPException):
                await member.edit(nick=entry["nick"] or None, reason="SYKE: back from AFK")
        away = span_label(time.time() - entry.get("since", time.time()))
        pings = entry.get("pings") or []
        lines = [f"<@{uid}> · [jump](https://discord.com/channels/{guild.id}/{cid}/{mid}) · <t:{int(ts)}:R>"
                 for uid, cid, mid, ts in pings[-10:]]
        body = f"You were away **{away}**."
        if lines:
            body += f"\n**{len(pings)}** ping{'s' if len(pings) != 1 else ''} while you were gone:\n" + "\n".join(lines)
        else:
            body += " Nobody pinged you."
        with contextlib.suppress(discord.HTTPException):
            await message.reply(embed=embed(f"👋 Welcome back, {member.display_name}", body, OK),
                                allowed_mentions=discord.AllowedMentions.none(), delete_after=60 if not lines else None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        data = self.afk_data(message.guild.id)
        if not data:
            return
        mine = data.get(str(message.author.id))
        if mine is not None:
            prefix = self.bot.display_prefix(message.guild.id)
            text = (message.content or "").lower()
            if not any(text.startswith(prefix + name) for name in ("afk", "away", "brb")) \
                    and time.time() - mine.get("since", 0) > 5:
                await self._back(message, mine, data)
                return
        targets = {u.id for u in message.mentions}
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        if isinstance(resolved, discord.Message):
            targets.add(resolved.author.id)
        targets.discard(message.author.id)
        changed = False
        for uid in targets:
            entry = data.get(str(uid))
            if entry is None:
                continue
            entry.setdefault("pings", []).append([message.author.id, message.channel.id, message.id, time.time()])
            entry["pings"] = entry["pings"][-AFK_PINGS_KEPT:]
            changed = True
            key = (message.channel.id, uid)
            if time.time() - self._afk_noticed.get(key, 0) >= AFK_NOTICE_GAP:
                self._afk_noticed[key] = time.time()
                member = message.guild.get_member(uid)
                name = member.display_name.removeprefix(AFK_TAG) if member else "They"
                with contextlib.suppress(discord.HTTPException):
                    await message.reply(embed=embed(f"💤 {name} is AFK",
                                                    f"{entry.get('reason') or 'AFK'} · since <t:{int(entry.get('since', 0))}:R>",
                                                    BRAND), allowed_mentions=discord.AllowedMentions.none())
        if changed:
            self.bot.storage.set_config(message.guild.id, AFK_KEY, data)

    # Birthdays

    def bdays(self, guild_id: int) -> dict:
        data = dict(self.bot.storage.get_config(guild_id, BDAY_KEY))
        data.setdefault("people", {})
        return data

    @commands.hybrid_group(name="birthday", aliases=["bday", "birthdays"], fallback="show", case_insensitive=True,
                           description="Set your birthday and SYKE celebrates it")
    @app_commands.describe(member="Whose birthday (default: yours)")
    @app_commands.guild_only()
    @commands.guild_only()
    async def birthday(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        who = member or ctx.author
        data = self.bdays(ctx.guild.id)
        stored = data["people"].get(str(who.id))
        p = self.p(ctx)
        if not stored:
            text = (f"Set yours with `{p}birthday set 24.12`." if who == ctx.author
                    else f"{who.display_name} hasn't set a birthday.")
            await ctx.reply(embed=embed("🎂 No birthday saved", text), ephemeral=True)
            return
        month, day = stored
        today = datetime.now(self.bot.tz_for(ctx.guild.id)).date()
        left = days_until(month, day, today)
        when = "**today!** 🎉" if left == 0 else f"in **{left}** day{'s' if left != 1 else ''}"
        await ctx.reply(embed=embed(f"🎂 {who.display_name}'s birthday", f"{birthday_text(month, day)}, {when}"))

    @birthday.command(name="set", description="Save your birthday (day and month)")
    @app_commands.describe(date="e.g. 24.12 or 24 December")
    async def birthday_set(self, ctx: commands.Context, *, date: str) -> None:
        parsed = parse_birthday(date)
        if parsed is None:
            await ctx.reply(embed=problem("Which day?", "Try `24.12`, `24/12` or `24 December`."), ephemeral=True)
            return
        data = self.bdays(ctx.guild.id)
        data["people"][str(ctx.author.id)] = list(parsed)
        self.bot.storage.set_config(ctx.guild.id, BDAY_KEY, data)
        extra = "" if data.get("channel") else (f"\n⚠️ No birthday channel is set yet. An admin can pick one with "
                                                f"`{self.p(ctx)}birthday channel #channel`.")
        await ctx.reply(embed=embed("🎂 Birthday saved", f"**{birthday_text(*parsed)}**. SYKE will celebrate you on "
                                                       f"the day.{extra}", OK), ephemeral=True)

    @birthday.command(name="remove", aliases=["clear", "delete"], description="Forget your birthday")
    async def birthday_remove(self, ctx: commands.Context) -> None:
        data = self.bdays(ctx.guild.id)
        data["people"].pop(str(ctx.author.id), None)
        self.bot.storage.set_config(ctx.guild.id, BDAY_KEY, data)
        await ctx.reply(embed=embed("Birthday removed", "SYKE won't celebrate it any more.", WARN), ephemeral=True)

    @birthday.command(name="list", aliases=["upcoming", "next"], description="The next birthdays in this server")
    async def birthday_list(self, ctx: commands.Context) -> None:
        data = self.bdays(ctx.guild.id)
        today = datetime.now(self.bot.tz_for(ctx.guild.id)).date()
        upcoming = []
        for uid, (month, day) in data["people"].items():
            if ctx.guild.get_member(int(uid)) is None:
                continue
            upcoming.append((days_until(month, day, today), int(uid), month, day))
        upcoming.sort()
        if not upcoming:
            await ctx.reply(embed=embed("🎂 No birthdays yet", f"Add yours with `{self.p(ctx)}birthday set 24.12`."))
            return
        lines = [f"<@{uid}> · {birthday_text(m, d)} · " + ("**today** 🎉" if left == 0 else f"in {left}d")
                 for left, uid, m, d in upcoming[:15]]
        await ctx.reply(embed=embed("🎂 Upcoming birthdays", "\n".join(lines)),
                        allowed_mentions=discord.AllowedMentions.none())

    @birthday.command(name="channel", description="Where SYKE posts birthday wishes")
    @app_commands.describe(channel="The birthday channel")
    @moderator("manage_guild")
    async def birthday_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        self.bot.storage.update_config(ctx.guild.id, BDAY_KEY, channel=channel.id)
        await ctx.reply(embed=embed("Birthday channel set 🎂", f"Wishes go to {channel.mention}. Birthday people "
                                                               f"get the **{BDAY_ROLE_NAME}** role for the day.", OK),
                        ephemeral=True)

    @birthday.command(name="role", description="Use your own role for birthday people (default: SYKE makes one)")
    @app_commands.describe(role="The birthday role")
    @moderator("manage_guild")
    async def birthday_role(self, ctx: commands.Context, role: discord.Role) -> None:
        if role >= ctx.guild.me.top_role or role.managed:
            await ctx.reply(embed=problem("Can't use that role", "It has to sit below SYKE's role."), ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, BDAY_KEY, role=role.id)
        await ctx.reply(embed=embed("Birthday role set", f"Birthday people get {role.mention}.", OK), ephemeral=True)

    @birthday.command(name="off", description="Stop birthday wishes in this server")
    @moderator("manage_guild")
    async def birthday_off(self, ctx: commands.Context) -> None:
        self.bot.storage.update_config(ctx.guild.id, BDAY_KEY, channel=None)
        await ctx.reply(embed=embed("Birthday wishes off", "Saved birthdays are kept.", WARN), ephemeral=True)

    async def _birthday_role(self, guild: discord.Guild, data: dict) -> discord.Role | None:
        role = guild.get_role(int(data.get("role") or 0))
        if role is not None:
            return role
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return None
        try:
            role = await guild.create_role(name=BDAY_ROLE_NAME, colour=discord.Colour.from_str(BDAY_COLOUR),
                                           hoist=True, reason="SYKE: birthdays")
        except discord.HTTPException:
            return None
        with contextlib.suppress(discord.HTTPException):
            await role.edit(position=max(1, me.top_role.position - 1))
        self.bot.storage.update_config(guild.id, BDAY_KEY, role=role.id)
        return role

    async def celebrate(self, guild: discord.Guild, now: datetime | None = None) -> int:
        """Wish today's birthday people, hand out the role, take yesterday's away. Returns how many wished."""
        data = self.bdays(guild.id)
        now = now or datetime.now(self.bot.tz_for(guild.id))
        today = now.date()
        done = dict(data.get("done") or {})
        holders = dict(data.get("holders") or {})
        role = guild.get_role(int(data.get("role") or 0))
        for uid, until in list(holders.items()):  # birthday role lasts 24 hours
            if until <= time.time():
                holders.pop(uid)
                member = guild.get_member(int(uid))
                if member is not None and role is not None and role in member.roles:
                    with contextlib.suppress(discord.HTTPException):
                        await member.remove_roles(role, reason="SYKE: birthday's over")
        channel = guild.get_channel(int(data.get("channel") or 0))
        wished = 0
        if channel is not None:
            for uid, (month, day) in data["people"].items():
                if not is_birthday(month, day, today) or done.get(uid) == today.isoformat():
                    continue
                member = guild.get_member(int(uid))
                if member is None:
                    continue
                done[uid] = today.isoformat()
                role = role or await self._birthday_role(guild, data)
                if role is not None:
                    with contextlib.suppress(discord.HTTPException):
                        await member.add_roles(role, reason="SYKE: happy birthday")
                        holders[uid] = time.time() + 86400
                gift = ""
                eco = self.bot.get_cog("Economy")
                if eco is not None and eco._open(guild.id):
                    self.bot.storage.econ_touch(guild.id, member.id, 0)
                    self.bot.storage.econ_adjust(guild.id, member.id, cash=BDAY_GIFT)
                    gift = f"\nThe Strip sends {eco.money(BDAY_GIFT)} as a gift. 🎁"
                card = embed(f"🎂 Happy birthday, {member.display_name}!",
                             f"Everyone wish {member.mention} a happy birthday! 🎉🥳{gift}",
                             discord.Colour.from_str(BDAY_COLOUR))
                card.set_thumbnail(url=member.display_avatar.url)
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(content=member.mention, embed=card,
                                       allowed_mentions=discord.AllowedMentions(users=[member]))
                    wished += 1
        data.update(done=done, holders=holders)
        self.bot.storage.set_config(guild.id, BDAY_KEY, data)
        return wished

    @tasks.loop(minutes=15)
    async def birthday_loop(self) -> None:
        for guild_id in list(self.bot.storage.configs(BDAY_KEY)):
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                try:
                    await self.celebrate(guild)
                except Exception:
                    log.exception("Birthday check failed in %s", guild_id)

    @birthday_loop.before_loop
    async def _before_birthdays(self) -> None:
        await self.bot.wait_until_ready()

    # Wrapped

    async def emoji_image(self, value: str | None) -> Image.Image | None:
        if not value:
            return None
        if value in self._emoji_cache:
            return self._emoji_cache[value]
        m = re.fullmatch(r"<(a?):\w+:(\d+)>", value)
        if m:
            url = f"https://cdn.discordapp.com/emojis/{m.group(2)}.png?size=128"
        else:
            points = [f"{ord(c):x}" for c in value]
            if "200d" not in points:
                points = [p for p in points if p != "fe0f"]
            url = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{'-'.join(points)}.png"
        data = await self.fetch(url)
        image = None
        if data:
            with contextlib.suppress(OSError):
                image = Image.open(io.BytesIO(data)).convert("RGBA")
        self._emoji_cache[value] = image
        return image

    async def wrapped(self, ctx: commands.Context, member: discord.Member | None) -> None:
        who = member or ctx.author
        guild = ctx.guild
        if who.bot or who.id in self.bot.storage.optouts(guild.id):
            await ctx.reply(embed=problem("No Wrapped", f"{who.display_name} isn't tracked by SYKE."), ephemeral=True)
            return
        tz = self.bot.tz_for(guild.id)
        year = datetime.now(tz).year
        since = datetime(year, 1, 1, tzinfo=tz).timestamp()
        async with ctx.typing():
            rows = await asyncio.to_thread(self.bot.storage.author_rows, guild.id, who.id, since)
            if len(rows) < 20:
                await ctx.reply(embed=problem("Not enough yet", f"SYKE has only {len(rows)} of {who.display_name}'s "
                                                                f"messages from {year}. It needs 20."), ephemeral=True)
                return
            channels = sorted({r[1] for r in rows})
            activity, counts = await asyncio.gather(
                asyncio.to_thread(self.bot.storage.channel_activity, guild.id, channels, since),
                asyncio.to_thread(self.bot.storage.author_counts, guild.id, since))
            optouts = self.bot.storage.optouts(guild.id)
            activity = [a for a in activity if a[1] not in optouts]
            partners = talk_partners(activity, who.id)
            ranked = [uid for uid, _ in counts if uid not in optouts]
            rank = (ranked.index(who.id) + 1, len(ranked)) if who.id in ranked else None
            names = {c.id: c.name for c in guild.channels}
            stats = wrapped_stats(rows, tz, rank, partners, names)
            avatar = await self.fetch(who.display_avatar.replace(size=256, static_format="png").url)
            icon = await self.emoji_image(stats.get("emoji"))
            png = await asyncio.to_thread(render_wrapped, who.display_name, year, stats, avatar, icon, guild.name)
        file = discord.File(io.BytesIO(png), filename=f"wrapped-{year}.png")
        card = discord.Embed(colour=discord.Colour.from_str("#EC4899"))
        card.set_image(url=f"attachment://wrapped-{year}.png")
        if stats.get("best"):
            best = discord.utils.escape_mentions(" ".join(stats["best"].split()))[:200]
            card.description = f"😂 Funniest message ({stats['best_laughs']} laughs):\n> {best}"
        card.set_footer(text=f"From {len(rows):,} stored messages this year")
        await ctx.reply(embed=card, file=file, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="wrapped", aliases=["recapcard", "yearreview"],
                      help="Your year on the server, Spotify Wrapped style")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.member)
    async def wrapped_cmd(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self.wrapped(ctx, member)

    @recap.command(name="wrapped", description="Your year on the server, Spotify Wrapped style")
    @app_commands.describe(member="Whose Wrapped (default: yours)")
    async def wrapped_slash(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        await self.wrapped(ctx, member)

    # Vibe

    async def recent_lines(self, ctx: commands.Context, limit: int) -> list[Line]:
        optouts = self.bot.storage.optouts(ctx.guild.id)
        lines: list[Line] = []
        kwargs = {"limit": limit * 2}
        origin = getattr(ctx, "message", None)
        if ctx.interaction is None and origin is not None:
            kwargs["before"] = origin
        with contextlib.suppress(discord.HTTPException):
            async for message in ctx.channel.history(**kwargs):
                line = from_discord_message(message)
                if line is None or message.author.id in optouts or self.bot.is_command_text(ctx.guild.id, line.text):
                    continue
                lines.append(line)
                if len(lines) >= limit:
                    break
        lines.reverse()
        return lines

    async def vibe(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            lines = await self.recent_lines(ctx, VIBE_MESSAGES)
            if len(lines) < 10:
                await ctx.reply(embed=problem("Too quiet", "There aren't enough recent messages here to read the vibe."),
                                ephemeral=True)
                return
            result, used_ai = None, False
            if self.bot.settings.ai_provider != "none":
                chat = "\n".join(f"{l.author[:20]}: {' '.join(l.text.split())[:160]}" for l in lines)
                with contextlib.suppress(Exception):
                    data = await ai._complete(self.bot.settings, VIBE_PROMPT, chat, 0.8, 20)
                    if isinstance(data.get("scores"), dict) and data.get("mood"):
                        result, used_ai = data, True
            result = result or offline_vibe(lines)
        scores = result.get("scores") or {}
        rows = "\n".join(f"`{k.title():<9}` `{bar(int(scores.get(k, 0) or 0))}` {int(scores.get(k, 0) or 0)}"
                         for k in VIBE_KEYS)
        mood = ai.public_text(result.get("mood"), 40) or "Unreadable"
        icon = ai.public_text(result.get("emoji"), 8) or "🌀"
        card = embed(f"{icon} Vibe check: {mood}", f"{ai.public_text(result.get('read'), 400)}\n\n{rows}")
        card.set_footer(text=f"Read from the last {len(lines)} messages in #{getattr(ctx.channel, 'name', 'here')}"
                        + ("" if used_ai else " · offline read"))
        await ctx.reply(embed=card, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="vibe", aliases=["vibecheck", "mood"], help="Read the last 100 messages and sum up the mood")
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.channel)
    async def vibe_cmd(self, ctx: commands.Context) -> None:
        await self.vibe(ctx)

    @recap.command(name="vibe", description="Read the last 100 messages here and sum up the mood")
    async def vibe_slash(self, interaction: discord.Interaction) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        await self.vibe(ctx)

    # Catch-up

    async def catchup(self, ctx: commands.Context) -> None:
        guild, me = ctx.guild, ctx.author
        optouts = self.bot.storage.optouts(guild.id)
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        lines: list[Line] = []
        mentions: list[discord.Message] = []
        gone_since = None
        kwargs = {"limit": CATCHUP_FETCH}
        if ctx.interaction is None and getattr(ctx, "message", None) is not None:
            kwargs["before"] = ctx.message
        async with ctx.typing():
            with contextlib.suppress(discord.HTTPException):
                async for message in ctx.channel.history(**kwargs):
                    if message.author.id == me.id and not self.bot.is_command_text(guild.id, message.content or ""):
                        gone_since = message.created_at.timestamp()
                        break
                    if me in message.mentions or (getattr(message.reference, "resolved", None) is not None and
                                                  getattr(message.reference.resolved, "author", None) == me):
                        mentions.append(message)
                    line = from_discord_message(message)
                    if line is None or message.author.id in optouts or self.bot.is_command_text(guild.id, line.text):
                        continue
                    lines.append(line)
            lines.reverse()
            since = gone_since or (lines[0].created if lines else time.time() - 86400)
            elsewhere = await asyncio.to_thread(self.bot.storage.mentions_since, guild.id, me.id, since, 10)
            elsewhere = [r for r in elsewhere if r[1] != ctx.channel.id and r[2] not in optouts]
            if len(lines) < CATCHUP_MIN and not mentions and not elsewhere:
                await ctx.reply(embed=embed("You're all caught up", "Barely anything happened here since you last "
                                                                   "talked.", OK), ephemeral=True)
                return
            summary = ""
            if len(lines) >= CATCHUP_MIN:
                facts = await asyncio.to_thread(digest, lines)
                sample = prompt_sample(lines)
                written = await ai.write_tldr(self.bot.settings, getattr(ctx.channel, "name", "channel"),
                                              [(l.author, l.text) for l in sample], len(lines), prompt_facts(facts))
                summary = written[0] if written else offline_summary(lines, getattr(ctx.channel, "name", "here"),
                                                                     facts)[0]
        away = span_label(time.time() - since) if since else ""
        title = f"🛋️ While you were gone" + (f" ({away}, {len(lines)} messages)" if away else "")
        card = embed(title, summary or "Not much was said here.")
        pinged = []
        for message in mentions[:5]:
            snippet = discord.utils.escape_mentions(" ".join((message.content or "").split()))[:80]
            pinged.append(f"**{message.author.display_name}** · [jump]({message.jump_url}) · {snippet}")
        for mid, cid, _, name, content, _created in elsewhere[:5]:
            snippet = discord.utils.escape_mentions(" ".join(re.sub(r"<@!?\d+>", "@you", content).split()))[:80]
            pinged.append(f"**{name or 'someone'}** in <#{cid}> · "
                          f"[jump](https://discord.com/channels/{guild.id}/{cid}/{mid}) · {snippet}")
        if pinged:
            card.add_field(name="📣 You were mentioned", value="\n".join(pinged)[:1024], inline=False)
        if not gone_since:
            card.set_footer(text="You haven't talked here recently, so this covers the latest messages.")
        await ctx.reply(embed=card, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="catchup", aliases=["missed", "whatdidimiss", "wdim"],
                      help="What happened here since you last talked, and who mentioned you")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.member)
    async def catchup_cmd(self, ctx: commands.Context) -> None:
        await self.catchup(ctx)

    @recap.command(name="catchup", description="What happened here since you last talked, and who mentioned you")
    async def catchup_slash(self, interaction: discord.Interaction) -> None:
        ctx = await commands.Context.from_interaction(interaction)
        await self.catchup(ctx)
