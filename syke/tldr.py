"""$tldr: what the last few hundred messages in this channel were actually about."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from .lexicon import STOPWORDS
from .models import Msg
from .stats import words_of
from .traits import score_message

# Summarise at least this many content messages when the channel has them, and never more than CAP.
TLDR_TARGET = 300
TLDR_CAP = 400
TLDR_FETCH = 900
SUMMARY_LIMIT = 1800
PROMPT_LIMIT = 340

_VIBES = {
    "funny": "Mostly bits. Someone was trying to be funny and it landed often enough.",
    "toxic": "Hostile. The chat was looking for a fight and found one.",
    "freaky": "Down bad. Read the recap somewhere you can close the tab.",
    "chaotic": "Unhinged. Caps lock was a lifestyle for a few hundred messages.",
    "cringe": "Cringe won. The brainrot is load-bearing.",
    "serious": "Weirdly sincere. People were actually saying things.",
}


@dataclass(frozen=True)
class Line:
    author: str
    text: str
    created: float = 0.0


def from_discord_message(message) -> Line | None:
    text = (getattr(message, "content", None) or "").strip()
    author = getattr(message, "author", None)
    if not text or getattr(author, "bot", False):
        return None
    created = getattr(message, "created_at", None)
    stamp = created.timestamp() if created is not None else 0.0
    name = getattr(author, "display_name", None) or getattr(author, "name", None) or "someone"
    return Line(str(name), text, stamp)


def assemble(newest_first: list[Line], older_newest_first: list[Line]) -> list[Line]:
    """Chronological lines: the recent ones, then older stored ones if that is still under 300."""
    chosen = list(newest_first[:TLDR_CAP])
    seen = {(line.author, line.text, round(line.created, 1)) for line in chosen}
    if len(chosen) < TLDR_TARGET:
        for line in older_newest_first:
            key = (line.author, line.text, round(line.created, 1))
            if key in seen:
                continue
            seen.add(key)
            chosen.append(line)
            if len(chosen) >= TLDR_CAP:
                break
    chosen.reverse()
    return chosen


def prompt_sample(lines: list[Line], limit: int = PROMPT_LIMIT) -> list[Line]:
    """A spread across the whole window, so a long recap is not only the last few minutes."""
    if len(lines) <= limit:
        return lines
    step = len(lines) / limit
    picked: list[Line] = []
    seen: set[tuple[str, str]] = set()
    for index in range(limit):
        line = lines[min(len(lines) - 1, int(index * step))]
        key = (line.author, line.text)
        if key in seen:
            continue
        seen.add(key)
        picked.append(line)
    for line in lines[-12:]:
        key = (line.author, line.text)
        if key not in seen:
            seen.add(key)
            picked.append(line)
    return picked


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def vibe_line(lines: list[Line]) -> str:
    if not lines:
        return "Suspiciously quiet."
    step = max(1, len(lines) // 80)
    sample = lines[::step][:80]
    totals: dict[str, float] = {trait: 0.0 for trait in _VIBES}
    for line in sample:
        when = datetime.fromtimestamp(line.created or 1, timezone.utc)
        values = score_message(Msg(0, line.author, line.text, when), timezone.utc)
        for trait in totals:
            totals[trait] += values[trait]
    best = max(totals, key=lambda trait: totals[trait])
    if totals[best] < 0.35 * len(sample):
        return "Nothing peaked. Suspiciously normal chat."
    return _VIBES[best]


def offline_summary(lines: list[Line], channel: str) -> tuple[str, str]:
    """A specific recap with no model: who talked, what repeated, two real lines."""
    counts = Counter(line.author for line in lines)
    loud = ", ".join(f"**{name}** ({count})" for name, count in counts.most_common(3)) or "nobody"
    words: list[str] = []
    for line in lines:
        words.extend(word for word in words_of(line.text) if word not in STOPWORDS and len(word) > 3)
    topics = [word for word, count in Counter(words).most_common(12) if count >= 3][:5]
    topic_bit = ", ".join(f"**{word}**" for word in topics) or "nothing you could explain sober"
    quotes: list[Line] = []
    seen: set[str] = set()
    for line in sorted(lines, key=lambda item: len(item.text), reverse=True):
        if line.author in seen or line.text.lower().startswith("http") or len(line.text.split()) < 4:
            continue
        seen.add(line.author)
        quotes.append(line)
        if len(quotes) == 2:
            break
    body = [
        f"**{len(lines):,}** messages in #{channel}. {loud} did most of the talking.",
        f"The chat kept circling {topic_bit}.",
    ]
    for quote in quotes:
        body.append(f"**{quote.author}:** \"{_clip(quote.text, 140)}\"")
    vibe = vibe_line(lines)
    return "\n\n".join(body)[:SUMMARY_LIMIT], vibe


def tldr_embed(summary: str, vibe: str, count: int, channel: str, ai_used: bool) -> discord.Embed:
    note = ""
    if count < TLDR_TARGET:
        note = (
            f"\n\nOnly **{count}** message{'s' if count != 1 else ''} to go on. "
            f"SYKE summarises {TLDR_TARGET} or more when the channel has them."
        )
    embed = discord.Embed(
        title=f"📝 TL;DR · #{channel}"[:256],
        description=(summary.strip() + note)[:4096],
        colour=discord.Colour.from_str("#8B5CF6"),
    )
    embed.add_field(name="Vibe", value=(vibe or "—")[:1024], inline=False)
    how = "AI" if ai_used else "offline"
    embed.set_footer(text=f"{count:,} messages • {how} • $tldr")
    return embed
