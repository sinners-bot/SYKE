"""$tldr: what the last few hundred messages in this channel were actually about."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import log2, sqrt

import discord

from .lexicon import CHAT_SLANG, LAUGH_EMOJIS, LAUGH_PHRASES, LAUGH_WORDS, STOPWORDS
from .models import Msg
from .stats import extract_emojis, words_of
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

# Long words that show up in every chat and never describe what it was about.
_FILLER = {
    "something", "anything", "nothing", "everything", "someone", "anyone", "everyone", "people",
    "thing", "things", "stuff", "literally", "actually", "basically", "honestly", "probably",
    "right", "never", "always", "though", "maybe", "good", "back", "time", "first", "last",
    "into", "after", "before", "again", "other", "every", "same", "look", "come", "came", "take",
    "tell", "told", "mean", "need", "feel", "keep", "give", "made", "went", "goes", "wait",
    "okay", "sure", "yall", "y'all", "with", "what's", "there's", "here's", "it'll", "would've",
    "wanna", "gotta", "kind", "sort", "pretty", "less", "most", "many", "next", "since",
    "while", "until", "around", "through", "hello", "thanks", "please", "today",
}
_LAUGH_CUTOFF = 3


@dataclass(frozen=True)
class Line:
    author: str
    text: str
    created: float = 0.0


@dataclass(frozen=True)
class Digest:
    """Everything SYKE can say about a window without a model, shared by the offline recap and the prompt."""

    count: int
    talkers: list[tuple[str, int]]
    topics: list[str]
    started: float = 0.0
    ended: float = 0.0
    arc: tuple[str, str] | None = None
    highlights: list[Line] = field(default_factory=list)

    @property
    def people(self) -> int:
        return len(self.talkers)

    @property
    def span(self) -> str:
        return span_label(self.ended - self.started) if self.started and self.ended > self.started else ""


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


def span_label(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _content_words(text: str) -> list[str | None]:
    """Words in order, with None where a word was too generic to be a topic, so phrases never bridge a gap."""
    kept: list[str | None] = []
    for word in words_of(text):
        word = word.strip("'")
        generic = (
            len(word) <= 3 or word in STOPWORDS or word in CHAT_SLANG or word in LAUGH_WORDS or word in _FILLER
        )
        kept.append(None if generic else word)
    return kept


def topics_of(lines: list[Line], limit: int = 5) -> list[str]:
    """What the chat kept returning to, weighted so one person spamming cannot define the topic.

    A term earns at most `cap` points per author and is boosted by how many people used it. Two-word
    phrases replace their words when they recur across different messages.
    """
    if not lines:
        return []
    cap = max(3, len(lines) // 10)
    repeats = Counter((line.author, " ".join(line.text.lower().split())) for line in lines)
    hits: dict[str, Counter] = defaultdict(Counter)
    phrase_texts: dict[str, set[str]] = defaultdict(set)
    for (author, text), times in repeats.items():
        words = _content_words(text)
        terms = {word for word in words if word}
        for left, right in zip(words, words[1:]):
            if left and right and left != right:
                phrase = f"{left} {right}"
                terms.add(phrase)
                phrase_texts[phrase].add(text)
        # Pasting the same line again adds a little, not a full message's worth.
        for term in terms:
            hits[term][author] += 1 + log2(times)

    for term in [term for term in hits if term.endswith("s") and " " not in term]:
        single = term[:-1]
        if single in hits:
            hits[single].update(hits.pop(term))

    def weight(term: str) -> float:
        per_author = hits[term]
        return sum(min(count, cap) for count in per_author.values()) * sqrt(len(per_author))

    floor = 3 if len(lines) >= 30 else 2
    phrases = [term for term in hits if " " in term and len(phrase_texts[term]) >= 2]
    ranked_phrases = sorted(phrases, key=weight, reverse=True)
    chosen: list[str] = []
    covered: set[str] = set()
    for phrase in ranked_phrases:
        left, right = phrase.split()
        if weight(phrase) < floor or left in covered or right in covered:
            continue
        if weight(phrase) >= 0.6 * max(weight(left), weight(right)):
            chosen.append(phrase)
            covered.update((left, right))
        if len(chosen) >= 2:
            break

    words = sorted((term for term in hits if " " not in term), key=lambda t: (-weight(t), t))
    for word in words:
        if len(chosen) >= limit or weight(word) < floor:
            break
        if word not in covered:
            chosen.append(word)
    order = {term: weight(term) for term in chosen}
    return sorted(chosen, key=lambda term: -order[term])[:limit]


def _arc(lines: list[Line]) -> tuple[str, str] | None:
    """(what it started on, what it ended on) when the conversation clearly moved."""
    if len(lines) < 30:
        return None
    third = len(lines) // 3
    early, late = topics_of(lines[:third], 3), topics_of(lines[-third:], 3)
    start = next((topic for topic in early if topic not in late), None)
    end = next((topic for topic in late if topic not in early), None)
    if start and end and not set(start.split()) & set(end.split()):
        return start, end
    return None


def _laughs(text: str) -> bool:
    lowered = text.lower()
    return (
        any(word in LAUGH_WORDS for word in words_of(text))
        or any(emoji in LAUGH_EMOJIS for emoji in extract_emojis(text))
        or any(phrase in lowered for phrase in LAUGH_PHRASES)
    )


def highlights(lines: list[Line], topics: list[str], limit: int = 2) -> list[Line]:
    """Lines worth quoting: ones other people laughed at or that carry a topic, one per author."""
    topic_words = {word for topic in topics for word in topic.split()}
    scored: list[tuple[float, int, Line]] = []
    for index, line in enumerate(lines):
        words = line.text.split()
        if len(words) < 4 or line.text.lower().startswith("http"):
            continue
        after = lines[index + 1:index + 1 + _LAUGH_CUTOFF]
        landed = sum(1 for reply in after if reply.author != line.author and _laughs(reply.text))
        on_topic = len(topic_words & set(words_of(line.text)))
        length = min(len(words), 25) / 25 - max(0, len(words) - 40) / 40
        scored.append((landed * 2.0 + min(on_topic, 2) + length, index, line))
    picked: list[tuple[int, Line]] = []
    authors: set[str] = set()
    texts: set[str] = set()
    for _score, index, line in sorted(scored, key=lambda item: (-item[0], item[1])):
        folded = " ".join(line.text.lower().split())
        if line.author in authors or folded in texts:
            continue
        authors.add(line.author)
        texts.add(folded)
        picked.append((index, line))
        if len(picked) == limit:
            break
    return [line for _index, line in sorted(picked, key=lambda item: item[0])]


def digest(lines: list[Line]) -> Digest:
    stamps = [line.created for line in lines if line.created]
    topics = topics_of(lines)
    return Digest(
        count=len(lines),
        talkers=Counter(line.author for line in lines).most_common(),
        topics=topics,
        started=min(stamps, default=0.0),
        ended=max(stamps, default=0.0),
        arc=_arc(lines),
        highlights=highlights(lines, topics),
    )


def _bold_list(items: list[str]) -> str:
    bold = [f"**{item}**" for item in items]
    if len(bold) <= 2:
        return " and ".join(bold)
    return ", ".join(bold[:-1]) + " and " + bold[-1]


def talkers_text(facts: Digest, limit: int = 3) -> str:
    if not facts.talkers:
        return "nobody"
    shown = [f"**{name}** · {count * 100 // max(1, facts.count)}%" for name, count in facts.talkers[:limit]]
    rest = facts.people - limit
    if rest > 0:
        shown.append(f"+{rest} other{'s' if rest != 1 else ''}")
    return "\n".join(shown)


def prompt_facts(facts: Digest) -> str:
    """Grounding the model can lean on instead of guessing who was loud or what repeated."""
    loud = ", ".join(f"{name} ({count})" for name, count in facts.talkers[:5]) or "nobody"
    rows = [f"Talkers by message count: {loud}; {facts.people} people in total."]
    if facts.span:
        rows.append(f"Time covered: {facts.span}.")
    if facts.topics:
        rows.append(f"Recurring terms: {', '.join(facts.topics)}.")
    if facts.arc:
        rows.append(f"Drift: early on it was {facts.arc[0]}, by the end it was {facts.arc[1]}.")
    for line in facts.highlights:
        rows.append(f"Stand-out line, {line.author}: {_clip(line.text, 120)}")
    return "\n".join(rows)


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


def offline_summary(lines: list[Line], channel: str, facts: Digest | None = None) -> tuple[str, str]:
    """A specific recap with no model: what it was about, how it moved, who ran it, the best lines."""
    facts = facts or digest(lines)
    about = (
        f"mostly about {_bold_list(facts.topics[:3])}" if facts.topics else "about nothing you could explain sober"
    )
    body = [f"**{facts.count:,}** messages in #{channel}, {about}."]
    if facts.arc:
        body.append(f"🧭 Started on **{facts.arc[0]}**, ended up on **{facts.arc[1]}**.")
    if facts.people >= 3 and facts.talkers[0][1] * 2 > facts.count:
        name, count = facts.talkers[0]
        body.append(f"🎤 **{name}** carried the chat with {count * 100 // facts.count}% of the messages.")
    elif facts.people:
        names = [name for name, _count in facts.talkers[:3]]
        body.append(f"🗣️ {_bold_list(names)} did most of the talking.")
    if facts.highlights:
        quotes = "\n".join(f"> **{line.author}:** {_clip(line.text, 140)}" for line in facts.highlights)
        body.append(f"📌 **Best lines**\n{quotes}")
    return "\n\n".join(body)[:SUMMARY_LIMIT], vibe_line(lines)


def tldr_embed(summary: str, vibe: str, facts: Digest, channel: str, ai_used: bool) -> discord.Embed:
    note = ""
    if facts.count < TLDR_TARGET:
        note = (
            f"\n\nOnly **{facts.count}** message{'s' if facts.count != 1 else ''} to go on. "
            f"SYKE summarises {TLDR_TARGET} or more when the channel has them."
        )
    embed = discord.Embed(
        title=f"📝 TL;DR · #{channel}"[:256],
        description=(summary.strip()[:4096 - len(note)] + note),
        colour=discord.Colour.from_str("#8B5CF6"),
    )
    embed.add_field(name="Vibe", value=(vibe or "—")[:1024], inline=False)
    embed.add_field(name="Loudest", value=talkers_text(facts)[:1024], inline=True)
    if facts.span:
        window = f"{facts.span}\nstarted <t:{int(facts.started)}:R>"
        embed.add_field(name="Window", value=window[:1024], inline=True)
    how = "AI" if ai_used else "offline"
    people = f"{facts.people} {'person' if facts.people == 1 else 'people'}"
    messages = f"{facts.count:,} message{'s' if facts.count != 1 else ''}"
    embed.set_footer(text=f"{messages} • {people} • {how} • $tldr")
    return embed
