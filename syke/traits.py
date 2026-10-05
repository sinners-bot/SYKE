"""Heuristic personality scoring.

Every message gets raw per-trait signals; a user's trait score is their average
signal squashed into 0-100. Because it's deterministic and cheap, SYKE can score
the whole server and rank people against each other without calling an AI.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import tzinfo

from . import lexicon as lx
from .models import Msg
from .stats import clean_text, extract_emojis, words_of

TRAITS: tuple[str, ...] = ("funny", "toxic", "cringe", "freaky", "serious", "chaotic")

TRAIT_META: dict[str, tuple[str, str]] = {
    "funny": ("😂", "Funny"),
    "toxic": ("☠️", "Toxic"),
    "cringe": ("💀", "Cringe"),
    "freaky": ("😏", "Freaky"),
    "serious": ("🧠", "Serious"),
    "chaotic": ("🔥", "Chaotic"),
}

# Higher = a smaller average signal is enough to push the score up.
SENSITIVITY: dict[str, float] = {
    "funny": 1.6,
    "toxic": 3.0,
    "cringe": 3.0,
    "freaky": 3.5,
    "serious": 1.4,
    "chaotic": 1.5,
}

KEYBOARD_SMASH_RE = re.compile(r"\b[asdfghjkl;]{6,}\b", re.IGNORECASE)
STRETCH_RE = re.compile(r"([a-z])\1{3,}", re.IGNORECASE)
PUNCT_SPAM_RE = re.compile(r"[!?]{3,}")
BURST_SECONDS = 8


@dataclass
class MessageSignals:
    msg: Msg
    values: dict[str, float]


def _caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 5:
        return 0.0
    return sum(c.isupper() for c in letters) / len(letters)


def score_message(msg: Msg, tz: tzinfo, in_burst: bool = False) -> dict[str, float]:
    content = msg.content
    lowered = content.lower()
    words = words_of(content)
    word_set = set(words)
    emojis = extract_emojis(content)
    caps = _caps_ratio(clean_text(content))
    hour = msg.created_at.astimezone(tz).hour

    laugh_hits = sum(w in lx.LAUGH_WORDS for w in words) + sum(e in lx.LAUGH_EMOJIS for e in emojis)
    funny = min(laugh_hits, 3) * 0.35 + min(msg.laugh_reactions, 6) * 0.6

    toxic_hits = sum(w in lx.TOXIC_WORDS for w in words) + sum(e in lx.TOXIC_EMOJIS for e in emojis)
    toxic = min(toxic_hits, 3) * 0.5 + (0.5 if toxic_hits and caps > 0.7 else 0.0)

    cringe = (
        len(word_set & lx.CRINGE_WORDS) * 0.6
        + sum(p in lowered for p in lx.CRINGE_PATTERNS) * 0.5
        + (0.4 if len(emojis) >= 4 else 0.0)
        + (0.3 if STRETCH_RE.search(content) else 0.0)
    )

    freaky = (
        len(word_set & lx.FREAKY_WORDS) * 0.6
        + sum(p in lowered for p in lx.FREAKY_PHRASES) * 0.8
        + min(sum(e in lx.FREAKY_EMOJIS for e in emojis), 3) * 0.5
    )

    serious = 0.0
    if len(words) >= 15:
        serious += 0.8
    elif len(words) >= 8:
        serious += 0.3
    serious += min(len(word_set & lx.SERIOUS_WORDS), 3) * 0.3
    if content[:1].isupper() and content.rstrip()[-1:] in {".", "?"}:
        serious += 0.2
    if laugh_hits or emojis:
        serious *= 0.5

    chaotic = (
        (0.8 if caps > 0.7 else 0.0)
        + (0.6 if PUNCT_SPAM_RE.search(content) else 0.0)
        + (1.0 if KEYBOARD_SMASH_RE.search(content) else 0.0)
        + (0.3 if STRETCH_RE.search(content) else 0.0)
        + (0.4 if hour < 5 else 0.0)
        + (0.4 if in_burst else 0.0)
    )

    return {
        "funny": funny,
        "toxic": toxic,
        "cringe": cringe,
        "freaky": freaky,
        "serious": serious,
        "chaotic": chaotic,
    }


def score_user(messages: list[Msg], tz: tzinfo) -> tuple[dict[str, int], list[MessageSignals]]:
    ordered = sorted(messages, key=lambda m: m.created_at)
    signals: list[MessageSignals] = []
    previous = None
    for msg in ordered:
        in_burst = previous is not None and (msg.created_at - previous).total_seconds() <= BURST_SECONDS
        signals.append(MessageSignals(msg, score_message(msg, tz, in_burst)))
        previous = msg.created_at

    count = max(len(signals), 1)
    scores: dict[str, int] = {}
    for trait in TRAITS:
        mean = sum(s.values[trait] for s in signals) / count
        scores[trait] = round(100 * (1 - math.exp(-mean * SENSITIVITY[trait])))
    return scores, signals


HIGHLIGHT_TRAITS: dict[str, str] = {
    "funniest": "funny",
    "unhinged": "chaotic",
    "freakiest": "freaky",
    "toxic": "toxic",
}


def pick_highlights(signals: list[MessageSignals]) -> dict[str, Msg | None]:
    """The single most representative message for each hall-of-fame slot."""
    picks: dict[str, Msg | None] = {}
    used: set[int] = set()
    for slot, trait in HIGHLIGHT_TRAITS.items():
        candidates = [
            s for s in signals
            if s.values[trait] > 0 and len(s.msg.content.strip()) >= 3 and id(s.msg) not in used
        ]
        if not candidates:
            picks[slot] = None
            continue
        best = max(candidates, key=lambda s: (s.values[trait], min(len(s.msg.content), 120)))
        used.add(id(best.msg))
        picks[slot] = best.msg
    return picks
