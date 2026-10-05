"""Hard numbers: volume, timing, message length, emojis, catchphrases."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

import emoji as emoji_lib

from .lexicon import STOPWORDS
from .models import Msg

CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
MENTION_RE = re.compile(r"<[@#][!&]?\d+>")
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[a-z][a-z']+")


@dataclass
class UserStats:
    message_count: int
    first_seen: datetime
    last_seen: datetime
    avg_words: float
    peak_start_hour: int
    peak_end_hour: int
    hour_histogram: list[int]
    top_emojis: list[tuple[str, int]] = field(default_factory=list)
    top_words: list[tuple[str, int]] = field(default_factory=list)
    top_phrases: list[tuple[str, int]] = field(default_factory=list)
    late_night_ratio: float = 0.0
    channels_used: int = 1

    @property
    def peak_label(self) -> str:
        return f"{self.peak_start_hour:02d}:00–{self.peak_end_hour:02d}:00"


def clean_text(content: str) -> str:
    text = URL_RE.sub(" ", content)
    text = MENTION_RE.sub(" ", text)
    return CUSTOM_EMOJI_RE.sub(" ", text)


def extract_emojis(content: str) -> list[str]:
    found = [f":{name}:" for name in CUSTOM_EMOJI_RE.findall(content)]
    found.extend(e["emoji"] for e in emoji_lib.emoji_list(content))
    return found


def words_of(content: str) -> list[str]:
    return WORD_RE.findall(clean_text(content).lower())


def _overlaps(a: str, b: str) -> bool:
    return bool(set(a.split()) & set(b.split()))


def peak_window(hours: list[int], width: int = 3) -> tuple[int, int]:
    """Busiest contiguous `width`-hour window, wrapping around midnight."""
    best_start, best_total = 0, -1
    for start in range(24):
        total = sum(hours[(start + i) % 24] for i in range(width))
        if total > best_total:
            best_start, best_total = start, total
    return best_start, (best_start + width) % 24


def compute_stats(messages: list[Msg], tz: tzinfo) -> UserStats:
    if not messages:
        raise ValueError("compute_stats needs at least one message")

    hours = [0] * 24
    emoji_counter: Counter[str] = Counter()
    word_counter: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()
    total_words = 0
    late_night = 0

    for msg in messages:
        local = msg.created_at.astimezone(tz)
        hours[local.hour] += 1
        if local.hour < 5:
            late_night += 1

        emoji_counter.update(extract_emojis(msg.content))

        words = words_of(msg.content)
        total_words += len(words)
        word_counter.update(w for w in words if len(w) >= 3 and w not in STOPWORDS)

        seen_in_msg: set[str] = set()
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                gram = words[i : i + n]
                if all(w in STOPWORDS for w in gram):
                    continue
                seen_in_msg.add(" ".join(gram))
        phrase_counter.update(seen_in_msg)

    start, end = peak_window(hours)
    candidates = [(p, c) for p, c in phrase_counter.most_common(40) if c >= 3]
    candidates.sort(key=lambda pc: (pc[1], len(pc[0].split())), reverse=True)
    trimmed: list[tuple[str, int]] = []
    for phrase, count in candidates:
        if sum(w in STOPWORDS for w in phrase.split()) * 2 > len(phrase.split()):
            continue
        if any(_overlaps(phrase, kept) for kept, _ in trimmed):
            continue
        trimmed.append((phrase, count))

    ordered = sorted(messages, key=lambda m: m.created_at)
    return UserStats(
        message_count=len(messages),
        first_seen=ordered[0].created_at,
        last_seen=ordered[-1].created_at,
        avg_words=total_words / len(messages),
        peak_start_hour=start,
        peak_end_hour=end,
        hour_histogram=hours,
        top_emojis=emoji_counter.most_common(5),
        top_words=word_counter.most_common(8),
        top_phrases=trimmed[:3],
        late_night_ratio=late_night / len(messages),
        channels_used=len({m.channel_id for m in messages}),
    )
