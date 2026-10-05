"""The paged profile card shown by !profile: Overview, Highlights and Ranks."""

from __future__ import annotations

import discord

from .profile import Profile
from .traits import TRAIT_META, TRAITS

BAR_CELLS = 10
MAX_QUOTE = 180

TRAIT_COLOURS: dict[str, discord.Colour] = {
    "funny": discord.Colour.from_str("#FACC15"),
    "toxic": discord.Colour.from_str("#EF4444"),
    "cringe": discord.Colour.from_str("#EC4899"),
    "freaky": discord.Colour.from_str("#A855F7"),
    "serious": discord.Colour.from_str("#3B82F6"),
    "chaotic": discord.Colour.from_str("#F97316"),
}
NEUTRAL = discord.Colour.from_str("#8B5CF6")

ARCHETYPES: dict[str, tuple[str, str]] = {
    "funny": ("The Class Clown", "Lives for the laugh react."),
    "toxic": ("The Menace", "Patience: not found."),
    "cringe": ("The Cringelord", "Has never once hesitated before pressing send."),
    "freaky": ("The Degenerate", "Messages cannot be read aloud in public."),
    "serious": ("The Philosopher", "Writes essays where a 'lol' would do."),
    "chaotic": ("The Wildcard", "Operates on pure impulse and caps lock."),
}
NPC = ("The NPC", "Aggressively, suspiciously normal.")

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

PAGES = ("overview", "highlights", "ranks")
PAGE_LABELS = {"overview": ("📊", "Overview"), "highlights": ("🏆", "Highlights"), "ranks": ("🥇", "Ranks")}


def dominant_trait(profile: Profile) -> str | None:
    trait = max(TRAITS, key=lambda t: profile.scores[t])
    return trait if profile.scores[trait] >= 25 else None


def archetype(profile: Profile) -> tuple[str, str]:
    trait = dominant_trait(profile)
    return ARCHETYPES[trait] if trait else NPC


def colour_for(profile: Profile) -> discord.Colour:
    trait = dominant_trait(profile)
    return TRAIT_COLOURS[trait] if trait else NEUTRAL


def bar(value: int) -> str:
    filled = round(value / 100 * BAR_CELLS)
    return "█" * filled + "░" * (BAR_CELLS - filled)


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
    st = profile.stats
    name, tagline = archetype(profile)
    embed = _base(profile, "overview", avatar_url, demo)
    embed.title = discord.utils.escape_markdown(profile.name)

    summary = "\n".join(f"> {line}" if line.strip() else ">" for line in profile.summary.strip().splitlines())
    embed.description = f"**{name}** — *{tagline}*\n\n{summary}"

    embed.add_field(name="💬 Messages", value=f"{st.message_count:,}")
    embed.add_field(name="📅 Active since", value=st.first_seen.strftime("%b %Y"))
    embed.add_field(name="🕐 Peak hours", value=st.peak_label)
    embed.add_field(name="✍️ Avg length", value=f"{round(st.avg_words)} words")
    emojis = " ".join(e for e, _ in st.top_emojis[:3]) or "None"
    embed.add_field(name="😀 Top emojis", value=emojis)
    if st.top_phrases:
        catchphrase = f"“{clean_quote(st.top_phrases[0][0])}”"
    elif st.top_words:
        catchphrase = f"“{clean_quote(st.top_words[0][0])}”"
    else:
        catchphrase = "Silence"
    embed.add_field(name="🗣️ Catchphrase", value=catchphrase)

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
        "highlights": highlights_page(profile, avatar_url, demo),
        "ranks": ranks_page(profile, avatar_url, demo),
    }


class ProfileView(discord.ui.View):
    """Page buttons. Only the person who ran the command can flip pages."""

    def __init__(self, pages: dict[str, discord.Embed], owner_id: int, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.owner_id = owner_id
        self.current = "overview"
        self.message: discord.Message | None = None
        for page in PAGES:
            emoji, label = PAGE_LABELS[page]
            button = discord.ui.Button(label=label, emoji=emoji, custom_id=f"syke:{page}")
            button.callback = self._make_callback(page)
            self.add_item(button)
        self._refresh()

    def _refresh(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                active = item.custom_id == f"syke:{self.current}"
                item.style = discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary
                item.disabled = active

    def _make_callback(self, page: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.current = page
            self._refresh()
            await interaction.response.edit_message(embed=self.pages[page], view=self)
        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            embed=self.pages[interaction.data.get("custom_id", "syke:overview").split(":")[1]],
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
