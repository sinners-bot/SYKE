"""Builds a full SYKE profile: stats + traits + highlights + achievements + server ranks."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import tzinfo

from .models import Msg
from .stats import UserStats, compute_stats
from .traits import TRAITS, MessageSignals, pick_highlights, score_user


@dataclass
class UserAnalysis:
    user_id: int
    name: str
    messages: list[Msg]
    stats: UserStats
    scores: dict[str, int]
    signals: list[MessageSignals]


@dataclass
class ServerAnalysis:
    users: dict[int, UserAnalysis]
    below_threshold: dict[int, int]  # user_id -> message count for people we can't judge yet

    def ranked(self, key: str) -> list[UserAnalysis]:
        if key == "active":
            return sorted(self.users.values(), key=lambda u: u.stats.message_count, reverse=True)
        return sorted(self.users.values(), key=lambda u: u.scores[key], reverse=True)

    def top_percent(self, user_id: int, key: str) -> int:
        """1-based 'Top X%' position of a user for a trait (or 'active')."""
        me = self.users[user_id]
        mine = me.stats.message_count if key == "active" else me.scores[key]
        values = [
            u.stats.message_count if key == "active" else u.scores[key]
            for u in self.users.values()
        ]
        better = sum(v > mine for v in values)
        return max(1, math.ceil((better + 1) / len(values) * 100))


@dataclass
class Profile:
    user_id: int
    name: str
    stats: UserStats
    scores: dict[str, int]
    highlights: dict[str, Msg | None]
    achievements: list[tuple[str, str]]
    server_ranks: dict[str, int]
    server_size: int
    summary: str = ""
    ai_used: bool = False
    messages: list[Msg] = field(default_factory=list, repr=False)
    ai_labels: dict[int, frozenset[str]] = field(default_factory=dict, repr=False)
    ai_scores: dict[str, int] = field(default_factory=dict)


def analyze_server(
    messages: list[Msg], tz: tzinfo, min_messages: int, labels: dict[int, frozenset[str]] | None = None
) -> ServerAnalysis:
    by_user: dict[int, list[Msg]] = defaultdict(list)
    for msg in messages:
        by_user[msg.author_id].append(msg)

    users: dict[int, UserAnalysis] = {}
    below: dict[int, int] = {}
    for user_id, msgs in by_user.items():
        if len(msgs) < min_messages:
            below[user_id] = len(msgs)
            continue
        scores, signals = score_user(msgs, tz, labels)
        latest_name = max(msgs, key=lambda m: m.created_at).author_name
        users[user_id] = UserAnalysis(user_id, latest_name, msgs, compute_stats(msgs, tz), scores, signals)
    return ServerAnalysis(users, below)


def award_achievements(user: UserAnalysis, server: ServerAnalysis) -> list[tuple[str, str]]:
    s, st = user.scores, user.stats
    active_rank = server.top_percent(user.user_id, "active")
    emoji_total = sum(c for _, c in st.top_emojis)

    rules: list[tuple[bool, str, str]] = [
        (s["toxic"] >= 35 and s["chaotic"] >= 50, "🏆", "Professional Menace"),
        (s["funny"] >= 65, "🤡", "Class Clown"),
        (st.late_night_ratio >= 0.3, "🌙", "Night Creature"),
        (s["chaotic"] >= 65, "💀", "Unhinged"),
        (s["freaky"] >= 50, "😈", "Suspicious Behavior"),
        (s["serious"] >= 60, "🧐", "Resident Philosopher"),
        (s["cringe"] >= 50, "🫠", "Certified Cringelord"),
        (s["toxic"] >= 60, "🧨", "Walking HR Violation"),
        (st.avg_words >= 22, "📜", "Essay Enjoyer"),
        (st.avg_words <= 4, "🏜️", "Dry Texter"),
        (emoji_total >= st.message_count * 0.6, "🎨", "Emoji Addict"),
        (active_rank <= 10 and len(server.users) >= 5, "🌱", "Should Touch Grass"),
        (st.channels_used >= 4, "🦋", "Social Butterfly"),
    ]
    earned = [(emoji, name) for ok, emoji, name in rules if ok]
    if not earned:
        earned.append(("🫥", "Aggressively Normal"))
    return earned[:6]


AI_SCORE_WEIGHT = 0.4


def refine_scores(profile: Profile, server: ServerAnalysis, tz: tzinfo, labels: dict[int, frozenset[str]]) -> None:
    """Re-score a member with AI message labels, blend in the AI's own ratings, and re-rank them."""
    scores, _ = score_user(profile.messages, tz, labels)
    if profile.ai_scores:
        scores = {
            t: round((1 - AI_SCORE_WEIGHT) * scores[t] + AI_SCORE_WEIGHT * profile.ai_scores.get(t, scores[t]))
            for t in TRAITS
        }
    profile.scores = scores
    others = [u for uid, u in server.users.items() if uid != profile.user_id]
    for trait in TRAITS:
        better = sum(u.scores[trait] > scores[trait] for u in others)
        profile.server_ranks[trait] = max(1, math.ceil((better + 1) / (len(others) + 1) * 100))


def build_profile(server: ServerAnalysis, user_id: int) -> Profile:
    user = server.users[user_id]
    ranks = {trait: server.top_percent(user_id, trait) for trait in TRAITS}
    ranks["active"] = server.top_percent(user_id, "active")
    return Profile(
        user_id=user.user_id,
        name=user.name,
        stats=user.stats,
        scores=user.scores,
        highlights=pick_highlights(user.signals),
        achievements=award_achievements(user, server),
        server_ranks=ranks,
        server_size=len(server.users),
        messages=user.messages,
    )
