"""`!showcase`: a tour of everything SYKE does, with live examples built from the demo members."""

from __future__ import annotations

import random
from datetime import timezone
from functools import lru_cache
from typing import Callable

import discord

from .cards import build_pages, iq_page
from .demo import build_demo_server
from .markov import build_chain
from .profile import ServerAnalysis, analyze_server, build_profile
from .render import render_leaderboard
from .roast import fallback_summary

BRAND = discord.Colour.from_str("#8B5CF6")
TOUR_ID = "syke:tour"
EXAMPLE_NOTE = "Example built from SYKE's fake demo members"

# (key, emoji, label, short description for the menu)
TOUR = [
    ("home", "👁️", "What is SYKE?", "The quick overview"),
    ("profile", "🧪", "Personality reports", "The full judgement card"),
    ("iq", "🧠", "IQ test", "A very unscientific IQ score"),
    ("top", "🏆", "Leaderboards", "Who's the funniest, most toxic, smartest"),
    ("talk", "🗣️", "Mimic & yap", "SYKE talks like your members"),
    ("data", "⛏️", "Your data", "Scanning, opting out, privacy"),
    ("admin", "⚙️", "Server setup", "Everything admins can configure"),
]


@lru_cache(maxsize=1)
def demo_server() -> ServerAnalysis:
    return analyze_server(build_demo_server(headline_volume=400), timezone.utc, 15)


def _page(title: str, body: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=body, colour=BRAND)
    embed.set_author(name="SYKE SHOWCASE")
    embed.set_footer(text="Pick another feature from the menu below")
    return embed


def tour_page(key: str, p: str) -> list[discord.Embed]:
    """The embeds for one showcase page; `p` is how commands start in this server (e.g. '!')."""
    if key == "profile":
        profile = build_profile(demo_server(), 1)
        profile.summary = fallback_summary(profile, random.Random(3))
        example = build_pages(profile, demo=True)["overview"]
        example.set_footer(text=f"{EXAMPLE_NOTE} • buttons flip to Full report, Highlights and Ranks")
        return [_page("🧪 Personality reports", (
            f"`{p}profile [@member]` reads someone's messages in the watched channels and judges them.\n\n"
            "• **Six traits**: funny, toxic, cringe, freaky, serious and chaotic, scored 0-100\n"
            "• **A roast** that quotes their real messages, written fresh every time\n"
            "• **Stats**: messages, peak hours (in *your* timezone), catchphrase, top emojis, IQ\n"
            "• **Highlights**: their funniest, most unhinged, freakiest and most toxic messages\n"
            "• **Ranks & achievements** against everyone else in the server\n\n"
            "Server emojis count too: `:KEKW:` reads as laughing."
        )), example]
    if key == "iq":
        server = demo_server()
        user = server.users[2]
        others = [u.iq.iq for uid, u in server.users.items() if uid != 2 and u.iq]
        example = iq_page(user.name, user.iq, round(sum(v < user.iq.iq for v in others) / len(others) * 100), demo=True)
        example.set_footer(text=EXAMPLE_NOTE)
        return [_page("🧠 IQ test", (
            f"`{p}iq [@member]` estimates an IQ from how someone writes: vocabulary, big words, message "
            "length and punctuation push it up; brainrot and caps-lock chaos drag it down.\n\n"
            f"It also shows on `{p}profile`, and `{p}top iq` ranks the whole server."
        )), example]
    if key == "top":
        board = discord.Embed(title="😂 Funny — server leaderboard", colour=BRAND,
                              description=f"```\n{render_leaderboard(demo_server(), 'funny')}\n```")
        board.set_footer(text=EXAMPLE_NOTE)
        return [_page("🏆 Leaderboards", (
            f"`{p}top [trait]` ranks everyone SYKE has judged.\n\n"
            "Traits: `funny` `toxic` `cringe` `freaky` `serious` `chaotic` `active` `iq`"
        )), board]
    if key == "talk":
        chain = build_chain([m.content for m in demo_server().users[1].messages])
        line = chain.generate(random.Random(5)) or "bro really thought he could solo the raid 💀"
        example = discord.Embed(description=line, colour=BRAND)
        example.set_author(name="Zyro (probably)")
        example.set_footer(text=f"{EXAMPLE_NOTE} • Markov chain • not a real quote")
        return [_page("🗣️ Mimic & yap", (
            f"`{p}mimic [@member]`: SYKE invents a message in someone's style from everything they've said.\n\n"
            f"`{p}yap on` (admins): now and then SYKE answers chat in that channel with something a member "
            "really said in the past. **Reply to it** and SYKE answers back with another message that fits, "
            "picked by AI."
        )), example]
    if key == "data":
        return [_page("⛏️ Your data", (
            f"`{p}scanme`: dig through older history so all your messages count towards your profile.\n"
            f"`{p}optout`: SYKE stops reading or judging you and deletes your stored messages. "
            f"`{p}optin` brings you back.\n\n"
            "SYKE only reads channels admins choose to watch, and ignores bots and commands. "
            "Reports are for fun only."
        ))]
    if key == "admin":
        return [_page("⚙️ Server setup (Manage Server)", (
            f"`{p}track #channel` / `{p}untrack #channel`: choose which channels SYKE reads\n"
            f"`{p}channels`: current settings\n"
            f"`{p}prefix <new|mention|reset>`: custom prefix, or @mention only\n"
            f"`{p}timezone <zone>`: what counts as late-night posting\n"
            f"`{p}yap on|off`: let SYKE chime in\n"
            f"`{p}collect [#channel]`: read more history for mimic and yap\n"
            f"`{p}rescan`: re-read channels now\n"
            f"`{p}demo on|off` and `{p}sample [name]`: fake members for testing alone\n"
            "`/diagnose`: why prefix commands might not work in a channel"
        ))]
    return [_page("👁️ SYKE — personality judgement, scientifically questionable", (
        "SYKE reads your server's chat and tells everyone what it thinks of them.\n\n"
        + "\n".join(f"{emoji} **{label}** — {desc}" for k, emoji, label, desc in TOUR if k != "home")
        + f"\n\n**Get started:** an admin runs `{p}track #general`, then anyone can run `{p}profile`.\n"
        "Slash commands work too. Pick a feature below to see it in action."
    ))]


class ShowcaseView(discord.ui.View):
    """Persistent menu, so the tour keeps working after restarts. Anyone can flip it."""

    def __init__(self, prefix_for: Callable[[int | None], str], invite_url: str | None, current: str = "home") -> None:
        super().__init__(timeout=None)
        self.prefix_for = prefix_for
        select = discord.ui.Select(
            custom_id=TOUR_ID,
            placeholder="Pick a feature to see it in action",
            options=[discord.SelectOption(label=label, value=key, emoji=emoji, description=desc, default=key == current)
                     for key, emoji, label, desc in TOUR],
        )
        select.callback = self._picked
        self.select = select
        self.add_item(select)
        self.invite_url = invite_url
        if invite_url:
            self.add_item(discord.ui.Button(label="Add SYKE to your server", emoji="➕", url=invite_url))

    async def _picked(self, interaction: discord.Interaction) -> None:
        key = self.select.values[0] if self.select.values else interaction.data.get("values", ["home"])[0]
        embeds = tour_page(key, self.prefix_for(interaction.guild_id))
        view = ShowcaseView(self.prefix_for, self.invite_url, key)
        await interaction.response.edit_message(embeds=embeds, view=view)
