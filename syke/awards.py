"""Weekly awards: who carried, ruined and haunted the chat over the last seven days."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

import discord

from .models import Msg
from .traits import score_user

BRAND = discord.Colour.from_str("#8B5CF6")
GOLD = discord.Colour.from_str("#F5B301")
WEEK = timedelta(days=7)
MIN_WEEK_MESSAGES = 8   # to be judged on traits
TRAIT_FLOOR = 15        # a trait score below this doesn't win anything
QUOTE_LIMIT = 140
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_ALIASES = {**{d: i for i, d in enumerate(DAYS)}, **{d[:3]: i for i, d in enumerate(DAYS)}}
DEFAULT_DAY = 6    # Sunday
DEFAULT_HOUR = 18  # 6pm, server timezone

# key, emoji, title, trait (None for counted awards), lines (one is picked; {name} and {value})
AWARDS: tuple[tuple[str, str, str, str | None, tuple[str, ...]], ...] = (
    ("yapper", "🗣️", "Biggest Yapper", None, (
        "{value} messages. Touch grass, then come back and tell us about it.",
        "{value} messages. The keyboard has filed a complaint.",
        "{value} messages. Some of them were even necessary.",
    )),
    ("funny", "😂", "Funniest of the Week", "funny", (
        "Carried the server's sense of humour on their back.",
        "Certified bit merchant. The chat would be a library without them.",
        "Made people exhale through their nose at least twice.",
    )),
    ("laughs", "💀", "Most Laughs Received", None, (
        "{value} 😂/💀 reactions. The people have spoken.",
        "Farmed {value} laugh reactions. Comedy is a business.",
    )),
    ("chaotic", "🌀", "Most Unhinged", "chaotic", (
        "Someone check on them. Gently. From a distance.",
        "Posted like the wifi was about to be cut off forever.",
    )),
    ("toxic", "☠️", "Most Toxic", "toxic", (
        "Nobody asked, and they answered anyway. Loudly.",
        "The chat's designated menace. Handle with tongs.",
    )),
    ("freaky", "😏", "Freakiest", "freaky", (
        "Down bad and not hiding it. Respectfully, log off.",
        "Every message had a wink in it somewhere.",
    )),
    ("cringe", "😬", "Cringe Lord", "cringe", (
        "Second-hand embarrassment, delivered weekly.",
        "Said things the rest of us will think about at 3am.",
    )),
    ("serious", "🧠", "Most Serious", "serious", (
        "Brought essays to a meme fight.",
        "The only adult in the room, apparently.",
    )),
    ("night", "🦉", "Night Owl", None, (
        "{value} messages between midnight and 5am. Sleep is a social construct.",
        "{value} messages after midnight. Their bed misses them.",
    )),
)


@dataclass
class Award:
    key: str
    emoji: str
    title: str
    user_id: int
    name: str
    line: str
    quote: str = ""


@dataclass
class WeeklyResult:
    awards: list[Award] = field(default_factory=list)
    top_message: Msg | None = None
    message_count: int = 0
    people: int = 0

    @property
    def winners(self) -> list[int]:
        seen: list[int] = []
        for award in self.awards:
            if award.user_id not in seen:
                seen.append(award.user_id)
        return seen


def _quote(msg: Msg | None) -> str:
    if msg is None:
        return ""
    text = " ".join(msg.content.split())
    if len(text) > QUOTE_LIMIT:
        text = text[: QUOTE_LIMIT - 1].rstrip() + "…"
    return text.replace("`", "'")


def compute_awards(messages: list[Msg], tz: tzinfo, labels: dict[int, frozenset[str]] | None = None,
                   *, rng: random.Random | None = None) -> WeeklyResult:
    """Awards from one week of messages. Each award needs a clear winner, so quiet weeks give fewer."""
    rng = rng or random.Random()
    by_user: dict[int, list[Msg]] = defaultdict(list)
    for msg in messages:
        by_user[msg.author_id].append(msg)
    result = WeeklyResult(message_count=len(messages), people=len(by_user))
    if not by_user:
        return result
    names = {uid: max(msgs, key=lambda m: m.created_at).author_name or "someone" for uid, msgs in by_user.items()}

    judged = {}
    for uid, msgs in by_user.items():
        if len(msgs) >= MIN_WEEK_MESSAGES:
            judged[uid] = score_user(msgs, tz, labels)

    counts = {uid: len(msgs) for uid, msgs in by_user.items()}
    laughs = {uid: sum(m.laugh_reactions for m in msgs) for uid, msgs in by_user.items()}
    night = {uid: sum(1 for m in msgs if m.created_at.astimezone(tz).hour < 5) for uid, msgs in by_user.items()}

    for key, emoji, title, trait, lines in AWARDS:
        quote_msg = None
        if trait is not None:
            if not judged:
                continue
            uid, (scores, signals) = max(judged.items(), key=lambda kv: (kv[1][0][trait], counts[kv[0]]))
            if scores[trait] < TRAIT_FLOOR:
                continue
            best = max(signals, key=lambda s: (s.values[trait], min(len(s.msg.content), 120)), default=None)
            quote_msg = best.msg if best is not None and best.values[trait] > 0 else None
            value = scores[trait]
        else:
            table = {"yapper": counts, "laughs": laughs, "night": night}[key]
            uid = max(table, key=lambda u: (table[u], counts[u]))
            value = table[uid]
            needed = {"yapper": 1, "laughs": 2, "night": 3}[key]
            if value < needed:
                continue
            if key == "laughs":
                quote_msg = max(by_user[uid], key=lambda m: m.laugh_reactions)
        line = rng.choice(lines).format(name=names[uid], value=value)
        result.awards.append(Award(key, emoji, title, uid, names[uid], line, _quote(quote_msg)))

    reacted = [m for m in messages if m.total_reactions > 0 and len(m.content.strip()) >= 3]
    if reacted:
        result.top_message = max(reacted, key=lambda m: (m.total_reactions, m.laugh_reactions))
    return result


def awards_embed(result: WeeklyResult, guild_name: str, *, role_mention: str | None = None,
                 preview: bool = False) -> discord.Embed:
    title = "🏆 SYKE Weekly Awards" + (" (preview)" if preview else "")
    if not result.awards:
        return discord.Embed(
            title=title, colour=BRAND,
            description="Nobody said enough this week to win anything. Tragic. Talk more.",
        )
    embed = discord.Embed(
        title=title, colour=GOLD,
        description=f"The lab reviewed **{result.message_count}** messages from **{result.people}** people "
                    f"in {guild_name} this week. The results are in.",
    )
    for award in result.awards[:24]:
        value = f"<@{award.user_id}> — {award.line}"
        if award.quote:
            value += f"\n> {award.quote}"
        embed.add_field(name=f"{award.emoji} {award.title}", value=value[:1024], inline=False)
    if result.top_message is not None and len(embed.fields) < 25:
        top = result.top_message
        embed.add_field(
            name="⭐ Message of the Week",
            value=f"<@{top.author_id}> ({top.total_reactions} reactions)\n> {_quote(top)}"[:1024],
            inline=False,
        )
    footer = "Every winner gets the role until next week's awards." if role_mention else "See you next week."
    embed.set_footer(text=footer)
    return embed


def parse_day(text: str) -> int | None:
    return DAY_ALIASES.get((text or "").strip().lower())


def parse_hour(text: str) -> int | None:
    """`18`, `18:00`, `6pm`, `6 pm` -> 18."""
    raw = (text or "").strip().lower().replace(" ", "")
    suffix = None
    if raw.endswith(("am", "pm")):
        suffix, raw = raw[-2:], raw[:-2]
    raw = raw.split(":")[0]
    if not raw.isdigit():
        return None
    hour = int(raw)
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    return hour if 0 <= hour <= 23 else None


def due_at(now: datetime, day: int, hour: int) -> datetime:
    """The most recent scheduled time at or before `now` (both in the server's timezone)."""
    scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=(now.weekday() - day) % 7)
    if scheduled > now:
        scheduled -= WEEK
    return scheduled


def schedule_text(config: dict) -> str:
    day = DAYS[config.get("day", DEFAULT_DAY)].title()
    hour = config.get("hour", DEFAULT_HOUR)
    return f"{day}s at {hour:02d}:00"
