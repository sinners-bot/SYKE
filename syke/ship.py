"""!ship: how two members match, from the way they actually talk."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from .profile import UserAnalysis
from .traits import TRAIT_META

QUOTE_LIMIT = 140
BLURB_LIMIT = 700
_TRAITS = ("funny", "freaky", "chaotic", "toxic", "serious", "cringe")


def ship_name(left: str, right: str) -> str:
    """First half of one name, second half of the other. Order is the order they were named."""
    left, right = left.strip(), right.strip()
    if not left or not right:
        return "Ship"
    head = left[: max(1, (len(left) + 1) // 2)]
    tail = right[len(right) // 2 :] or right[-1:]
    return (head + tail).strip()[:32] or "Ship"


def pair_jitter(left_id: int, right_id: int) -> int:
    """-5..5 for this pair, stable across processes."""
    lo, hi = sorted((left_id, right_id))
    mixed = (lo * 1315423911) ^ (hi * 2654435761)
    return (mixed % 11) - 5


def match_percent(left: UserAnalysis, right: UserAnalysis) -> int:
    """0-99. Shared habits count, and so does both of them actually having the habit."""
    sa, sb = left.scores, right.scores

    def close(trait: str) -> float:
        return 100 - abs(sa[trait] - sb[trait])

    shared = (
        close("funny") * 0.30
        + close("freaky") * 0.22
        + close("chaotic") * 0.20
        + close("serious") * 0.16
        + close("toxic") * 0.12
    )
    mutual = (
        min(sa["funny"], sb["funny"]) * 0.32
        + min(sa["freaky"], sb["freaky"]) * 0.30
        + min(sa["chaotic"], sb["chaotic"]) * 0.22
        + min(sa["toxic"], sb["toxic"]) * 0.16
    )
    # A composed person and a crashout is a romcom, up to a point.
    romcom = min(abs(sa["chaotic"] - sb["chaotic"]), 55) / 55 * 100
    raw = 0.40 * shared + 0.42 * mutual + 0.18 * romcom
    nudged = raw + pair_jitter(left.user_id, right.user_id) * 0.45
    return max(1, min(99, round(nudged)))


def match_label(score: int, left: UserAnalysis, right: UserAnalysis) -> str:
    sa, sb = left.scores, right.scores
    if min(sa["freaky"], sb["freaky"]) >= 42 and score >= 52:
        return "HR is already writing the email"
    if min(sa["toxic"], sb["toxic"]) >= 48 and score >= 48:
        return "Enemies to lovers, mostly enemies"
    if min(sa["chaotic"], sb["chaotic"]) >= 55 and score >= 50:
        return "Two crashouts, one bit"
    if score >= 84:
        return "Disgustingly compatible"
    if score >= 68:
        return "This could actually work"
    if score >= 52:
        return "Situationship with extra steps"
    if score >= 36:
        return "The group chat will have opinions"
    return "Do not pass go"


def heart_bar(score: int) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "❤️" * filled + "🖤" * (10 - filled)


def _clip(text: str, limit: int = QUOTE_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def signature_line(user: UserAnalysis) -> str:
    """A line that actually sounds like them, not their most recent 'ok'."""
    best = ""
    best_value = 0.0
    for signal in user.signals:
        text = signal.msg.content.strip()
        if len(text) < 3:
            continue
        value = max(signal.values[trait] for trait in _TRAITS)
        if value > best_value:
            best, best_value = text, value
    if best and best_value >= 0.45:
        return _clip(best)
    if user.stats.top_phrases:
        return _clip(user.stats.top_phrases[0][0])
    for msg in reversed(user.messages):
        if msg.content.strip():
            return _clip(msg.content)
    return ""


def voice_sample(user: UserAnalysis, limit: int = 36) -> list[str]:
    """Spikiest lines first, then recent ones, for the ship write-up."""
    ranked = sorted(
        user.signals,
        key=lambda s: (
            s.values["freaky"] + s.values["toxic"] + s.values["chaotic"]
            + s.values["funny"] + s.values["cringe"],
            len(s.msg.content),
        ),
        reverse=True,
    )
    picked: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        clean = " ".join(text.split())
        key = clean.casefold()
        if len(clean) < 2 or key in seen:
            return
        seen.add(key)
        picked.append(clean[:180])

    for signal in ranked:
        add(signal.msg.content)
        if len(picked) >= limit // 2:
            break
    for msg in reversed(user.messages):
        add(msg.content)
        if len(picked) >= limit:
            break
    return picked


def why_text(left: UserAnalysis, right: UserAnalysis) -> str:
    rows = []
    for trait in _TRAITS:
        mutual = min(left.scores[trait], right.scores[trait])
        gap = abs(left.scores[trait] - right.scores[trait])
        rows.append((mutual - gap * 0.3, trait, mutual, gap))
    rows.sort(reverse=True)
    lines = []
    for _, trait, mutual, gap in rows[:3]:
        emoji, name = TRAIT_META[trait]
        if gap >= 35:
            lines.append(f"{emoji} **{name}** — {left.scores[trait]} vs {right.scores[trait]}")
        else:
            lines.append(f"{emoji} **{name}** — both around {mutual}")
    return "\n".join(lines)


def offline_blurb(left: UserAnalysis, right: UserAnalysis, score: int, label: str) -> str:
    quote_a = signature_line(left) or "absolutely nothing, which is its own red flag"
    quote_b = signature_line(right) or "absolutely nothing, which is its own red flag"
    top = max(
        _TRAITS,
        key=lambda trait: min(left.scores[trait], right.scores[trait])
        - abs(left.scores[trait] - right.scores[trait]) * 0.25,
    )
    trait = TRAIT_META[top][1].lower()
    return (
        f"{left.name} and {right.name} match at {score}% on {trait} energy, and neither of them "
        f"is going to be normal about it. {left.name} really sent \"{quote_a}\". {right.name} "
        f"answered, spiritually, \"{quote_b}\". {label}."
    )


@dataclass
class ShipReading:
    score: int
    label: str
    ship_name: str
    blurb: str
    ai_used: bool
    left_quote: str
    right_quote: str
    why: str


def read_ship(left: UserAnalysis, right: UserAnalysis) -> ShipReading:
    score = match_percent(left, right)
    label = match_label(score, left, right)
    return ShipReading(
        score=score,
        label=label,
        ship_name=ship_name(left.name, right.name),
        blurb=offline_blurb(left, right, score, label),
        ai_used=False,
        left_quote=signature_line(left) or "—",
        right_quote=signature_line(right) or "—",
        why=why_text(left, right),
    )


def ship_embed(
    reading: ShipReading,
    left_name: str,
    right_name: str,
    left_count: int,
    right_count: int,
    avatar_url: str | None = None,
) -> discord.Embed:
    if reading.score >= 75:
        colour = discord.Colour.from_str("#F43F5E")
    elif reading.score >= 50:
        colour = discord.Colour.from_str("#A855F7")
    else:
        colour = discord.Colour.from_str("#64748B")
    embed = discord.Embed(
        title=f"💘 {left_name} × {right_name}"[:256],
        colour=colour,
        description=(
            f"## {reading.score}%  {heart_bar(reading.score)}\n"
            f"**{reading.label}**\n"
            f"Ship name: **{reading.ship_name}**\n\n"
            f"{reading.blurb}"
        )[:4096],
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.add_field(name=left_name[:256], value=f"\"{reading.left_quote}\""[:1024], inline=False)
    embed.add_field(name=right_name[:256], value=f"\"{reading.right_quote}\""[:1024], inline=False)
    if reading.why:
        embed.add_field(name="Why SYKE thinks so", value=reading.why[:1024], inline=False)
    source = "AI, from both of their chats" if reading.ai_used else "their actual chats"
    embed.set_footer(
        text=f"From {left_count:,} + {right_count:,} messages • {source} • for fun only"
    )
    return embed
