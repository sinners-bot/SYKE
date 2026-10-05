"""The paged profile card shown by !profile: Overview, Highlights and Ranks."""

from __future__ import annotations

import logging
import random
from typing import Callable

import discord

from .profile import Profile
from .traits import TRAIT_META, TRAITS

log = logging.getLogger("syke.cards")

BAR_CELLS = 10
MAX_QUOTE = 180
MESSAGE_VIEW_TIMEOUT = 600

TRAIT_COLOURS: dict[str, discord.Colour] = {
    "funny": discord.Colour.from_str("#FACC15"),
    "toxic": discord.Colour.from_str("#EF4444"),
    "cringe": discord.Colour.from_str("#EC4899"),
    "freaky": discord.Colour.from_str("#A855F7"),
    "serious": discord.Colour.from_str("#3B82F6"),
    "chaotic": discord.Colour.from_str("#F97316"),
}
NEUTRAL = discord.Colour.from_str("#8B5CF6")

ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    "funny": ("The Class Clown", ["Lives for the laugh react.", "Every chat is an open mic.",
                                  "Has a bit for everything.", "Comedy first, consequences never."]),
    "toxic": ("The Menace", ["Patience: not found.", "Types with clenched fists.",
                             "Has a ratio for every occasion.", "HR's most wanted."]),
    "cringe": ("The Cringelord", ["Has never once hesitated before pressing send.", "Brainrot, fluent.",
                                  "Second-hand embarrassment on tap.", "Says 'slay' unironically."]),
    "freaky": ("The Degenerate", ["Messages cannot be read aloud in public.", "Down bad, professionally.",
                                  "The mods keep a separate folder.", "Every chat is after midnight."]),
    "serious": ("The Philosopher", ["Writes essays where a 'lol' would do.", "Brings sources to a meme fight.",
                                    "Well, actually.", "Reads the whole message. Every time."]),
    "chaotic": ("The Wildcard", ["Operates on pure impulse and caps lock.", "Types like they're being chased.",
                                 "Eight messages, one thought.", "A jump scare with a keyboard."]),
}
NPC = ("The NPC", ["Aggressively, suspiciously normal.", "Default settings, never changed.",
                   "Could be a bot. We checked twice.", "Background character energy."])

HIGHLIGHTS = [
    ("funniest", "😂 Funniest message", "None found. Tragic."),
    ("unhinged", "💀 Most unhinged", "Suspiciously composed."),
    ("freakiest", "😈 Freakiest moment", "Clean record. For now."),
    ("toxic", "☠️ Most toxic moment", "Somehow, a saint."),
]

RANK_ROWS = [
    ("funny", "😂 Funniest"),
    ("active", "💬 Most active"),
    ("chaotic", "🔥 Most chaotic"),
    ("toxic", "☠️ Most toxic"),
    ("freaky", "😏 Most freaky"),
    ("cringe", "💀 Most cringe"),
    ("serious", "🧠 Most serious"),
]

PAGES = ("overview", "report", "highlights", "ranks")
PAGE_LABELS = {
    "overview": ("📊", "Overview"),
    "report": ("📋", "Full report"),
    "highlights": ("🏆", "Highlights"),
    "ranks": ("🥇", "Ranks"),
}
SHORT_SUMMARY = 260
TOP_TRAITS_SHOWN = 3
MINI_BAR_CELLS = 8


def dominant_trait(profile: Profile) -> str | None:
    trait = max(TRAITS, key=lambda t: profile.scores[t])
    return trait if profile.scores[trait] >= 25 else None


def archetype(profile: Profile, rng: random.Random | None = None) -> tuple[str, str]:
    trait = dominant_trait(profile)
    name, taglines = ARCHETYPES[trait] if trait else NPC
    return name, (rng or random.Random()).choice(taglines)


def colour_for(profile: Profile) -> discord.Colour:
    trait = dominant_trait(profile)
    return TRAIT_COLOURS[trait] if trait else NEUTRAL


def bar(value: int, cells: int = BAR_CELLS) -> str:
    filled = round(value / 100 * cells)
    return "█" * filled + "░" * (cells - filled)


def short_summary(summary: str, limit: int = SHORT_SUMMARY) -> str:
    """The first paragraph, cut at a sentence end if it's still too long."""
    first = next((p.strip() for p in summary.strip().split("\n\n") if p.strip()), "")
    first = " ".join(first.split())
    if len(first) <= limit:
        return first
    cut = first[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 3 else cut.rstrip() + "…"


def catchphrase(profile: Profile) -> str | None:
    st = profile.stats
    if st.top_phrases:
        return clean_quote(st.top_phrases[0][0])
    if st.top_words:
        return clean_quote(st.top_words[0][0])
    return None


def clean_quote(text: str) -> str:
    text = discord.utils.escape_markdown(" ".join(text.split()))
    if len(text) > MAX_QUOTE:
        text = text[: MAX_QUOTE - 1].rstrip() + "…"
    return text


def rank_badge(top_percent: int) -> str:
    if top_percent <= 10:
        return f"**Top {top_percent}%** 🔥"
    if top_percent <= 25:
        return f"**Top {top_percent}%**"
    return f"Top {top_percent}%"


def _base(profile: Profile, page: str, avatar_url: str | None, demo: bool) -> discord.Embed:
    emoji, label = PAGE_LABELS[page]
    embed = discord.Embed(colour=colour_for(profile))
    embed.set_author(name=f"SYKE REPORT · {emoji} {label}")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    engine = "AI analysis" if profile.ai_used else "offline analysis"
    footer = f"Page {PAGES.index(page) + 1}/{len(PAGES)} • {engine} • for entertainment only"
    if demo:
        footer += " • demo mode"
    embed.set_footer(text=footer)
    return embed


def overview_page(profile: Profile, avatar_url: str | None = None, demo: bool = False) -> discord.Embed:
    """The compact card posted in chat: verdict, one-paragraph roast, key numbers and top traits."""
    st = profile.stats
    name, tagline = archetype(profile)
    embed = _base(profile, "overview", avatar_url, demo)
    embed.title = discord.utils.escape_markdown(profile.name)

    facts = [f"💬 {st.message_count:,} msgs", f"🕐 {st.peak_label}", f"✍️ {round(st.avg_words)} words"]
    extras = []
    if st.top_emojis:
        extras.append(" ".join(e for e, _ in st.top_emojis[:3]))
    phrase = catchphrase(profile)
    if phrase:
        extras.append(f"🗣️ “{phrase}”")
    top = sorted(TRAITS, key=lambda t: profile.scores[t], reverse=True)[:TOP_TRAITS_SHOWN]
    rows = [f"{TRAIT_META[t][0]} {TRAIT_META[t][1]:<8} {bar(profile.scores[t], MINI_BAR_CELLS)} {profile.scores[t]:>3}%"
            for t in top]

    lines = [f"**{name}** — *{tagline}*", "", f"> {short_summary(profile.summary)}", "", " · ".join(facts)]
    if extras:
        lines.append(" · ".join(extras))
    lines.append("```\n" + "\n".join(rows) + "\n```")
    embed.description = "\n".join(lines)
    return embed


def report_page(profile: Profile, avatar_url: str | None = None, demo: bool = False) -> discord.Embed:
    st = profile.stats
    name, tagline = archetype(profile)
    embed = _base(profile, "report", avatar_url, demo)
    embed.title = discord.utils.escape_markdown(profile.name)

    summary = "\n".join(f"> {line}" if line.strip() else ">" for line in profile.summary.strip().splitlines())
    embed.description = f"**{name}** — *{tagline}*\n\n{summary}"

    embed.add_field(name="💬 Messages", value=f"{st.message_count:,}")
    embed.add_field(name="📅 Active since", value=st.first_seen.strftime("%b %Y"))
    embed.add_field(name="🕐 Peak hours", value=st.peak_label)
    embed.add_field(name="✍️ Avg length", value=f"{round(st.avg_words)} words")
    emojis = " ".join(e for e, _ in st.top_emojis[:3]) or "None"
    embed.add_field(name="😀 Top emojis", value=emojis)
    phrase = catchphrase(profile)
    embed.add_field(name="🗣️ Catchphrase", value=f"“{phrase}”" if phrase else "Silence")

    rows = []
    for trait in TRAITS:
        emoji, label = TRAIT_META[trait]
        rows.append(f"{emoji} {label:<8} {bar(profile.scores[trait])} {profile.scores[trait]:>3}%")
    embed.add_field(name="🎭 Personality", value="```\n" + "\n".join(rows) + "\n```", inline=False)
    return embed


def highlights_page(profile: Profile, avatar_url: str | None = None, demo: bool = False) -> discord.Embed:
    embed = _base(profile, "highlights", avatar_url, demo)
    embed.title = f"{discord.utils.escape_markdown(profile.name)}'s Hall of Fame"
    embed.description = "The messages that define them. Evidence has been preserved."
    for slot, label, empty in HIGHLIGHTS:
        msg = profile.highlights.get(slot)
        value = f"> {clean_quote(msg.content)}" if msg else f"*{empty}*"
        embed.add_field(name=label, value=value, inline=False)
    return embed


def ranks_page(profile: Profile, avatar_url: str | None = None, demo: bool = False) -> discord.Embed:
    embed = _base(profile, "ranks", avatar_url, demo)
    embed.title = f"{discord.utils.escape_markdown(profile.name)}'s Achievements"
    achievements = "\n".join(f"{emoji} **{discord.utils.escape_markdown(name)}**" for emoji, name in profile.achievements)
    embed.add_field(name="🏅 Unlocked", value=achievements or "*Nothing yet.*", inline=False)

    if profile.server_size >= 3:
        lines = [f"{label} — {rank_badge(profile.server_ranks[key])}" for key, label in RANK_ROWS]
        embed.add_field(
            name=f"📈 Compared with {profile.server_size} judged members",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="📈 Compared with the server",
            value="*Not enough judged members yet. Get more people talking.*",
            inline=False,
        )
    return embed


def build_pages(profile: Profile, avatar_url: str | None = None, demo: bool = False) -> dict[str, discord.Embed]:
    return {
        "overview": overview_page(profile, avatar_url, demo),
        "report": report_page(profile, avatar_url, demo),
        "highlights": highlights_page(profile, avatar_url, demo),
        "ranks": ranks_page(profile, avatar_url, demo),
    }


CardLoader = Callable[[int], "tuple[int, dict[str, discord.Embed]] | None"]


class ProfileView(discord.ui.View):
    """Page buttons for every profile card. Only the person who ran the command can flip pages.

    Pages are looked up by message id, and one view with no timeout is registered at startup, so
    buttons keep working after SYKE restarts. Views attached to single messages time out only so they
    don't pile up in memory; the registered one picks up clicks after that.
    """

    def __init__(self, load: CardLoader, current: str = "overview", timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)
        self.load = load
        for page in PAGES:
            emoji, label = PAGE_LABELS[page]
            active = page == current
            button = discord.ui.Button(
                label=label, emoji=emoji, custom_id=f"syke:{page}", disabled=active,
                style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            )
            button.callback = self._make_callback(page)
            self.add_item(button)

    def _make_callback(self, page: str):
        async def callback(interaction: discord.Interaction) -> None:
            card = self.load(interaction.message.id) if interaction.message else None
            if card is None:
                await interaction.response.send_message(
                    "This card has expired. Run the profile command again for a fresh one.", ephemeral=True
                )
                return
            owner_id, pages = card
            embed = pages.get(page) or pages["overview"]
            if interaction.user.id != owner_id:
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            view = ProfileView(self.load, page, timeout=MESSAGE_VIEW_TIMEOUT)
            await interaction.response.edit_message(embed=embed, view=view)
        return callback

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("Profile card button failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Couldn't flip the page. Try again.", ephemeral=True)
