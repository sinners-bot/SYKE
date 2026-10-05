"""A completely unscientific 'IQ' estimate from how someone writes."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from . import lexicon as lx
from .models import Msg
from .stats import UserStats, clean_text, words_of

IQ_MIN, IQ_MAX = 55, 160
LONG_WORD = 8
VOCAB_WINDOW = 100  # unique-word share is averaged over fixed windows so message count doesn't skew it
VOCAB_BASELINE = 0.7
BRAINROT_RE = re.compile(r"^\W*(lol|lmao|lmfao|xd|bruh|ok|k|ya|ye|w|l|real|fr|💀|😭|😂)+\W*$", re.IGNORECASE)


@dataclass
class IQFactor:
    emoji: str
    name: str
    detail: str
    points: int


@dataclass
class IQResult:
    iq: int
    label: str
    tagline: str
    factors: list[IQFactor] = field(default_factory=list)
    smartest: Msg | None = None
    dumbest: Msg | None = None


BANDS: list[tuple[int, str, list[str]]] = [
    (145, "🌌 Galaxy Brain", ["Has definitely corrected a teacher.", "Thinks in footnotes.",
                              "Their keyboard has a PhD."]),
    (130, "🧠 Big Brain", ["Uses words the rest of chat has to google.", "Suspiciously articulate.",
                           "Reads the terms and conditions."]),
    (115, "📚 Book Smart", ["Knows where the commas go.", "Above average, and won't let you forget it.",
                            "Owns at least one bookshelf."]),
    (100, "🙂 Perfectly Average", ["The control group.", "Statistically unremarkable.",
                                   "Brain running on default settings."]),
    (85, "🥴 Running on Vibes", ["Thinks in emojis.", "Gets by on confidence alone.",
                                 "Brain buffering, please wait."]),
    (70, "🪫 Low Battery", ["One braincell, working overtime.", "Has lost a fight to a door labelled 'pull'.",
                            "Brain set to airplane mode."]),
    (0, "🌡️ Room Temperature", ["Thinks Shrek is a documentary.", "The braincell called in sick.",
                                "Scientists are studying how they type at all."]),
]


def band(iq: int) -> tuple[str, list[str]]:
    for floor, label, lines in BANDS:
        if iq >= floor:
            return label, lines
    return BANDS[-1][1], BANDS[-1][2]


def message_smarts(msg: Msg) -> float:
    """How clever a single message looks: long words, length, care and serious vocabulary."""
    words = words_of(msg.content)
    if not words:
        return -2.0
    long_words = sum(len(w) >= LONG_WORD for w in words)
    serious = len(set(words) & lx.SERIOUS_WORDS) + sum(p in msg.content.lower() for p in lx.SERIOUS_PHRASES)
    brainrot = len(set(words) & lx.CRINGE_WORDS) + (1 if BRAINROT_RE.match(msg.content) else 0)
    text = clean_text(msg.content).strip()
    tidy = 0.5 if text[:1].isupper() and text[-1:] in {".", "?", "!"} else 0.0
    return min(len(words), 30) / 10 + long_words * 0.6 + serious * 0.5 + tidy - brainrot * 0.8


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def vocabulary(words: list[str]) -> float | None:
    """Average share of unique words per 100-word window, or None if there's too little text."""
    windows = [words[i:i + VOCAB_WINDOW] for i in range(0, len(words) - VOCAB_WINDOW + 1, VOCAB_WINDOW)]
    if not windows:
        return None
    return sum(len(set(w)) / len(w) for w in windows) / len(windows)


def compute_iq(messages: list[Msg], stats: UserStats, scores: dict[str, int],
               rng: random.Random | None = None) -> IQResult:
    words = [w for m in messages for w in words_of(m.content)]
    vocab = vocabulary(words)
    avg_len = _ratio(sum(len(w) for w in words), len(words))
    long_share = _ratio(sum(len(w) >= LONG_WORD for w in words), len(words))
    texts = [clean_text(m.content).strip() for m in messages]
    tidy = _ratio(sum(1 for t in texts if t[:1].isupper() and t[-1:] in {".", "?", "!"}), len(texts))
    rot = _ratio(sum(1 for m in messages if BRAINROT_RE.match(m.content)), len(messages))

    factors = [
        IQFactor("📚", "Vocabulary",
                 f"{round(vocab * 100)}% unique words" if vocab is not None else "not enough words yet",
                 max(-15, min(15, round((vocab - VOCAB_BASELINE) * 60))) if vocab is not None else 0),
        IQFactor("🔤", "Big words", f"{round(long_share * 100)}% are {LONG_WORD}+ letters, avg {avg_len:.1f}",
                 round((long_share - 0.08) * 120 + (avg_len - 4.2) * 6)),
        IQFactor("✍️", "Message length", f"{round(stats.avg_words)} words on average",
                 round(min(stats.avg_words - 7, 15) * 0.8)),
        IQFactor("🧐", "Writes properly", f"{round(tidy * 100)}% start with a capital and end with punctuation",
                 round((tidy - 0.15) * 20)),
        IQFactor("🧠", "Serious takes", f"serious {scores.get('serious', 0)}%",
                 round((scores.get("serious", 0) - 20) * 0.15)),
        IQFactor("🤪", "Brainrot", f"cringe {scores.get('cringe', 0)}%, {round(rot * 100)}% one-word reactions",
                 -round(scores.get("cringe", 0) * 0.12 + rot * 25)),
        IQFactor("🔥", "Caps-lock chaos", f"chaotic {scores.get('chaotic', 0)}%",
                 -round(max(scores.get("chaotic", 0) - 20, 0) * 0.1)),
    ]
    iq = max(IQ_MIN, min(IQ_MAX, 100 + sum(f.points for f in factors)))

    judged = [m for m in messages if len(words_of(m.content)) >= 2]
    smartest = max(judged, key=message_smarts, default=None)
    dumbest = min((m for m in judged if m is not smartest), key=message_smarts, default=None)
    label, lines = band(iq)
    return IQResult(iq, label, (rng or random.Random()).choice(lines), factors, smartest, dumbest)


def bell_curve(iq: int, width: int = 21) -> str:
    """A tiny bell curve with an arrow under the member's position."""
    curve = "▁▁▂▃▄▅▆▇██▇▆▅▄▃▂▁▁▁▁"
    curve = curve[:width].ljust(width, "▁")
    pos = round((min(max(iq, IQ_MIN), IQ_MAX) - IQ_MIN) / (IQ_MAX - IQ_MIN) * (width - 1))
    marker = " " * pos + "▲"
    scale = f"{IQ_MIN}".ljust(width // 2 - 1) + "100".ljust(width - width // 2 - 2) + f"{IQ_MAX}"
    return f"{curve}\n{marker}\n{scale}"
