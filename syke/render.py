"""Turns a Profile into the monospace 'lab report' card shown in Discord."""

from __future__ import annotations

import textwrap

from .profile import Profile, ServerAnalysis, metric
from .traits import TRAIT_META, TRAITS

WIDTH = 34  # fits a Discord code block on mobile without wrapping
BAR_CELLS = 12
DIVIDER = "─" * WIDTH
MAX_QUOTE = 70

HIGHLIGHT_LABELS = {
    "funniest": ("😂", "Funniest message"),
    "unhinged": ("💀", "Most unhinged message"),
    "freakiest": ("😈", "Freakiest moment"),
    "toxic": ("☠️", "Most toxic moment"),
}

EMPTY_HIGHLIGHT = {
    "funniest": "None found. Tragic.",
    "unhinged": "Suspiciously composed.",
    "freakiest": "Clean record. For now.",
    "toxic": "Somehow, a saint.",
}

COMPARE_LABELS = [
    ("funny", "Funniest"),
    ("active", "Most active"),
    ("chaotic", "Most chaotic"),
    ("toxic", "Most toxic"),
    ("freaky", "Most freaky"),
]


def bar(value: int) -> str:
    filled = round(value / 100 * BAR_CELLS)
    return "█" * filled + "░" * (BAR_CELLS - filled)


def safe(text: str) -> str:
    """Keep user text from breaking out of the code block."""
    return text.replace("```", "'''").replace("\n", " ").strip()


def quote(text: str) -> str:
    text = safe(text)
    if len(text) > MAX_QUOTE:
        text = text[: MAX_QUOTE - 1].rstrip() + "…"
    return "\n".join(textwrap.wrap(f'"{text}"', WIDTH)) or '""'


def wrap_paragraphs(text: str) -> str:
    paragraphs = [p.strip() for p in safe_multiline(text).split("\n\n") if p.strip()]
    return "\n\n".join("\n".join(textwrap.wrap(p, WIDTH)) for p in paragraphs)


def safe_multiline(text: str) -> str:
    return text.replace("```", "'''").replace("\r", "")


def render_report(profile: Profile) -> str:
    st = profile.stats
    lines: list[str] = []

    lines += ["📊 ACTIVITY"]
    lines += [f"Messages analyzed: {st.message_count:,}"]
    lines += [f"Active since: {st.first_seen.strftime('%B %Y')}"]
    lines += [f"Most active: {st.peak_label}"]
    lines += [f"Average message length: {round(st.avg_words)} words"]
    if st.top_emojis:
        lines += ["Top emojis: " + " ".join(f"{e}×{c}" for e, c in st.top_emojis[:3])]
    if st.top_phrases:
        lines += ["Catchphrases:"]
        lines += [f'  "{safe(p)[:WIDTH - 6]}" ×{c}' for p, c in st.top_phrases[:2]]
    elif st.top_words:
        lines += ["Top words: " + ", ".join(safe(w)[:12] for w, _ in st.top_words[:2])]

    lines += ["", DIVIDER, "", "🎭 PERSONALITY BREAKDOWN"]
    for trait in TRAITS:
        emoji, label = TRAIT_META[trait]
        score = profile.scores[trait]
        lines += [f"{emoji} {label:<8}{bar(score)} {score:>3}%"]

    lines += ["", DIVIDER, "", "🏆 YOUR STATS"]
    for slot, (emoji, label) in HIGHLIGHT_LABELS.items():
        msg = profile.highlights.get(slot)
        lines += ["", f"{emoji} {label}:"]
        lines += [quote(msg.content) if msg else EMPTY_HIGHLIGHT[slot]]

    lines += ["", DIVIDER, "", "🧬 PROFILE SUMMARY", "", wrap_paragraphs(profile.summary)]

    lines += ["", DIVIDER, "", "🥇 ACHIEVEMENTS"]
    lines += [f"{emoji} {name}" for emoji, name in profile.achievements]

    lines += ["", DIVIDER, "", "📈 Compared with the server"]
    if profile.server_size >= 3:
        lines += [""]
        for key, label in COMPARE_LABELS:
            lines += [f"{label + ':':<14}Top {profile.server_ranks[key]}%"]
    else:
        lines += ["", "Not enough judged members yet.", "Get more people talking."]

    return "\n".join(lines)


def render_leaderboard(server: ServerAnalysis, key: str, limit: int = 10) -> str:
    rows = server.ranked(key)[:limit]
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, user in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1:>2}."
        if key == "active":
            value = f"{user.stats.message_count:,} msgs"
        elif key == "iq":
            value = f"IQ {metric(user, key)}"
        else:
            value = f"{user.scores[key]}%"
        name = safe(user.name)[:18]
        out.append(f"{prefix} {name:<18} {value:>10}")
    return "\n".join(out) or "Nobody qualifies yet."
