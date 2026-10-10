"""A completely unscientific 'aura' reading from how someone writes."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from . import lexicon as lx
from .models import Msg
from .stats import UserStats, words_of

AURA_MIN, AURA_MAX = 0, 200
# Short, dry lines farm aura; essays and brainrot spend it.
IDEAL_WORDS = 5
YAP_WORDS = 9
BRAINROT_RE = re.compile(r"^\W*(lol|lmao|lmfao|xd|bruh|fr|💀|😭|😂)+\W*$", re.IGNORECASE)
W_WORDS = {"w", "based", "valid", "real", "facts", "true", "goated", "aura"}
L_WORDS = {"l", "ratio", "mid", "cope", "seethe", "cringe", "npc"}


@dataclass
class AuraFactor:
    emoji: str
    name: str
    detail: str
    points: int


@dataclass
class AuraResult:
    aura: int
    label: str
    tagline: str
    factors: list[AuraFactor] = field(default_factory=list)
    highest: Msg | None = None
    lowest: Msg | None = None


BANDS: list[tuple[int, str, list[str]]] = [
    (170, "🌌 Mythical Aura", ["Don't look directly at them.", "The room goes quiet when they type.",
                               "Main character, final season."]),
    (145, "✨ Aura Farmer", ["Collects aura like it's a seasonal event.", "Everything they say slaps.",
                             "Could post 'ok' and still win."]),
    (120, "😎 Main Character", ["Walks in with their own soundtrack.", "The chat orbits them.",
                                "W's only, apparently."]),
    (100, "🙂 Ambient Aura", ["Not blinding, not bankrupt.", "Background glow, default settings.",
                              "Present. That's the review."]),
    (75, "🫥 NPC Energy", ["Speaks only when spoken to, and still fumbles it.", "Extra in their own life.",
                           "The plot forgot they were here."]),
    (45, "📉 Aura Deficit", ["Every message is a withdrawal.", "Has to check the balance. It's overdrawn.",
                             "The opposite of mysterious."]),
    (0, "💀 Aura Bankruptcy", ["Filed for aura Chapter 11.", "Scientists are studying the deficit.",
                               "They typed, and the room got worse."]),
]


def band(aura: int) -> tuple[str, list[str]]:
    for floor, label, lines in BANDS:
        if aura >= floor:
            return label, lines
    return BANDS[-1][1], BANDS[-1][2]


def _ratio(part: float, whole: int) -> float:
    return part / whole if whole else 0.0


def message_aura(msg: Msg) -> float:
    """How much aura a single message farms or spends."""
    words = words_of(msg.content)
    n = len(words)
    if not n:
        return -1.0
    unique = set(words)
    brevity = 2.2 if n <= 6 else (0.4 if n <= 12 else -min((n - 12) * 0.12, 2.5))
    laughs = min(msg.laugh_reactions, 6) * 0.7
    cringe = len(unique & lx.CRINGE_WORDS) + sum(p in msg.content.lower() for p in lx.CRINGE_PATTERNS)
    toxic = len(unique & lx.TOXIC_WORDS)
    funny = len(unique & lx.LAUGH_WORDS)
    wins = len(unique & W_WORDS)
    losses = len(unique & L_WORDS)
    rot = 1.5 if BRAINROT_RE.match(msg.content) and n <= 2 else 0.0
    return brevity + laughs + wins * 0.6 + funny * 0.25 - cringe * 0.9 - toxic * 0.35 - losses * 0.5 - rot


def compute_aura(messages: list[Msg], stats: UserStats, scores: dict[str, int],
                 rng: random.Random | None = None) -> AuraResult:
    laughs = sum(m.laugh_reactions for m in messages)
    laugh_rate = _ratio(laughs, len(messages))
    rot = _ratio(sum(1 for m in messages if BRAINROT_RE.match(m.content)), len(messages))
    words = [w for m in messages for w in words_of(m.content)]
    w_share = _ratio(sum(w in W_WORDS for w in words), len(words))
    l_share = _ratio(sum(w in L_WORDS for w in words), len(words))

    mystery = round((IDEAL_WORDS - stats.avg_words) * 3.4)
    mystery = max(-24, min(24, mystery if stats.avg_words <= YAP_WORDS else mystery - 10))
    factors = [
        AuraFactor("🕯️", "Mystery", f"{round(stats.avg_words)} words on average", mystery),
        AuraFactor("😂", "Presence", f"{laughs} laugh reacts, {round(laugh_rate * 100)}% of messages",
                   round(min(laugh_rate, 1.2) * 22 + (scores.get("funny", 0) - 25) * 0.12)),
        AuraFactor("🫠", "Cringe tax", f"cringe {scores.get('cringe', 0)}%",
                   -round(scores.get("cringe", 0) * 0.28)),
        AuraFactor("🔥", "Composure", f"chaotic {scores.get('chaotic', 0)}%",
                   -round(max(scores.get("chaotic", 0) - 25, 0) * 0.16)),
        AuraFactor("📜", "Yap tax", f"{round(stats.avg_words)} words, {stats.message_count:,} messages",
                   -round(max(stats.avg_words - YAP_WORDS, 0) * 1.1)),
        AuraFactor("🏆", "W's", f"{round(w_share * 100)}% W words, {round(l_share * 100)}% L words",
                   round((w_share - l_share) * 80 + (scores.get("funny", 0) - scores.get("toxic", 0)) * 0.08)),
        AuraFactor("🌙", "Night creature", f"{round(stats.late_night_ratio * 100)}% after midnight",
                   round((stats.late_night_ratio - 0.12) * 18)),
        AuraFactor("🤪", "Brainrot", f"{round(rot * 100)}% one-word reactions",
                   -round(rot * 28 + scores.get("freaky", 0) * 0.08)),
    ]
    aura = max(AURA_MIN, min(AURA_MAX, 100 + sum(f.points for f in factors)))

    judged = [m for m in messages if words_of(m.content)]
    highest = max(judged, key=message_aura, default=None)
    lowest = min((m for m in judged if m is not highest), key=message_aura, default=None)
    label, lines = band(aura)
    return AuraResult(aura, label, (rng or random.Random()).choice(lines), factors, highest, lowest)


def bell_curve(aura: int, width: int = 21) -> str:
    """A tiny bell curve with an arrow under the member's position."""
    curve = "▁▁▂▃▄▅▆▇██▇▆▅▄▃▂▁▁▁▁"
    curve = curve[:width].ljust(width, "▁")
    pos = round((min(max(aura, AURA_MIN), AURA_MAX) - AURA_MIN) / (AURA_MAX - AURA_MIN) * (width - 1))
    marker = " " * pos + "▲"
    scale = f"{AURA_MIN}".ljust(width // 2 - 1) + "100".ljust(width - width // 2 - 2) + f"{AURA_MAX}"
    return f"{curve}\n{marker}\n{scale}"
