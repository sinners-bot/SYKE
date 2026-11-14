"""A clean, categorised, interactive help menu for SYKE.

`help()` builds everything from the bot's *live* commands, so signatures,
aliases and descriptions never drift from the real thing. Commands are sorted
into categories by the CATALOG below; a test asserts every registered command
appears in exactly one category, so nothing can silently go undocumented.

The menu itself is a dropdown (discord.ui.Select): pick a category, read a tidy
embed of its commands. `help <command>` shows a detailed card for one command
or group (description, usage, aliases, sub-commands, who can use it).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from .moderation import BRAND, OK, embed

# Access levels, shown on each category and command so people know what they
# can actually run.
EVERYONE = "Everyone"
EMOJIS = "Manage Emojis & Stickers"
MODERATOR = "Moderators"
ADMIN = "Admins (Manage Server)"
OWNER = "SYKE owners"


@dataclass
class Category:
    key: str
    emoji: str
    title: str
    access: str
    blurb: str
    commands: list[str] = field(default_factory=list)
    # Optional richer text / examples for a command, keyed by its name.
    details: dict[str, str] = field(default_factory=dict)


# Every top-level command/group name belongs to exactly one category.
CATALOG: list[Category] = [
    Category(
        "profile", "🧠", "Profile & Vibes", EVERYONE,
        "SYKE's (gloriously unscientific) read on people, built from what they post.",
        ["profile", "iq", "aura", "ship", "top", "tldr", "mimic", "dexify"],
        {
            "profile": "The full personality report for someone — traits, a verdict, the works. "
                       "Defaults to you if you don't @mention anyone.",
            "ship": "Rates how well two people match from how they chat. "
                    "`ship` alone picks two random members; give one or two @mentions to aim it.",
            "top": "Server leaderboard for a trait. Try: funny, toxic, cringe, freaky, "
                   "serious, chaotic, active, iq, aura.",
            "tldr": "Summarises the recent conversation in this channel so you can catch up fast.",
            "mimic": "SYKE talks like someone, using a Markov chain of their messages. Needs some history — "
                     "run `collect` or `scanme` if it's thin.",
            "dexify": "Reply to a message and SYKE reposts it with dramatic… pedro… pauses.",
        },
    ),
    Category(
        "ai", "🤖", "AI & Chat", EVERYONE,
        "Talk to SYKE. @mention it anywhere and it yaps back in the server's own voice.",
        ["ask", "yap"],
        {
            "ask": "Ask SYKE anything, like ChatGPT — works in DMs too. You can also just @mention it.",
            "yap": "Admin toggle: let SYKE chime into conversations on its own, in the server's voice. "
                   "@mention it or reply to it and it always answers.",
        },
    ),
    Category(
        "games", "🎮", "Fun & Games", EVERYONE,
        "Little games and text toys to mess around with. Reply to a message or pass your own text.",
        ["guess", "drunk", "scramble", "zalgo", "uwu", "mock", "keysmash"],
        {
            "guess": "\"Who said this?\" — SYKE quotes a real message and you guess the author. "
                     "`guess top` shows the best guessers.",
            "drunk": "Slurs the text like they've had a few — doubled letters, dropped letters, the odd *hic*.",
            "scramble": "Shuffles the middle of each word but keeps the first and last letter — still readable, barely.",
            "zalgo": "Corrupts the text into c̸u̷r̶s̵e̴d̷ glitch text.",
            "uwu": "Twanslates the text into uwu-speak, wiw faces and aww.",
            "mock": "aLtErNaTiNg mOcKiNg CaPs, SpongeBob style.",
            "keysmash": "Letters collapse into keyboard mash the further along they go.",
        },
    ),
    Category(
        "emoji", "😀", "Emojis & Stickers", EMOJIS,
        "Grow your server's emoji and sticker collection in seconds.",
        ["emoji", "sticker", "steal"],
        {
            "emoji": "Add a custom emoji from an attached image, a pasted link, or a replied-to message. "
                     "Sub-commands: `list`, `remove`, `rename`.",
            "sticker": "Same as emoji, but for stickers. Sub-commands: `list`, `remove`, `rename`.",
            "steal": "Reply to any message and SYKE adds all the custom emojis and stickers it used to this server.",
        },
    ),
    Category(
        "mod", "🛡️", "Moderation", MODERATOR,
        "Hands-on moderation tools. Every action is logged as a numbered case.",
        ["timeout", "untimeout", "warn", "warnings", "history", "delwarn", "clearwarns",
         "purge", "slowmode", "lock", "unlock", "kick", "ban", "unban", "tempban", "softban",
         "restrict", "unrestrict", "restrictions", "modlog"],
        {
            "timeout": "Temporarily mute a member for a duration like `10m`, `2h`, `1d` (Discord's max is 28 days).",
            "tempban": "Ban a member now and automatically unban them after the given duration.",
            "softban": "Ban then immediately unban — a quick way to kick someone *and* wipe their recent messages.",
            "restrict": "Strip a member of a capability server-wide — images, embeds/links, reactions, "
                        "external emojis, voice, nicknames — without a full mute. `restrict @member images embeds`.",
            "purge": "Bulk-delete recent messages in this channel; add an @member to only delete theirs.",
            "lock": "Stop @everyone from talking in a channel. `unlock` reverses it.",
            "history": "A member's full moderation record — warnings, timeouts, bans and automod cases.",
            "modlog": "Choose the channel every moderation and automod action gets logged to. `modlog off` to stop.",
        },
    ),
    Category(
        "automod", "🚦", "Automod", ADMIN,
        "Automatic rule-enforcement that runs on every message. Mods are never caught.",
        ["automod", "filter", "links", "antispam"],
        {
            "automod": "Overview of all automod rules, plus `automod ignore @role` to exempt a role and "
                       "`automod toxic #channel` to flag toxic messages for review instead of deleting.",
            "filter": "A word/slur blacklist with smart matching (beats leetspeak and spacing). "
                      "`filter on`, `filter add <word>`, `filter action delete|warn|timeout`.",
            "links": "Control invites and links. `links invites` blocks Discord invites, `links urls` blocks "
                     "non-allowlisted links, `links allow <domain>` permits one.",
            "antispam": "Catches flooding, repeated messages, mass-mentions and all-caps walls, then times the "
                        "spammer out. Tune with `antispam rate`, `antispam mentions`, `antispam duration`.",
        },
    ),
    Category(
        "security", "🚨", "Server Security", ADMIN,
        "Heavy-duty protection against nukes and raids. Off by default — turn on what you need.",
        ["antinuke", "antiraid", "raidmode"],
        {
            "antinuke": "Stops a rogue admin or compromised account from nuking the server. If one person mass-deletes "
                        "channels or roles, or mass-bans/kicks members in seconds, SYKE instantly neutralises them "
                        "(strips their roles or bans them). Whitelist people you trust with "
                        "`antinuke whitelist @user`.",
            "antiraid": "Watches for join floods — lots of accounts (often brand-new) joining at once. When it trips, "
                        "SYKE flips on raid mode and kicks or bans the wave. Tune with `antiraid rate` and "
                        "`antiraid age`.",
            "raidmode": "Manually flip raid mode on or off right now — locks the gate on new joiners until you call "
                        "the all-clear, for when you can see a raid coming.",
        },
    ),
    Category(
        "setup", "⚙️", "Setup & Settings", ADMIN,
        "Configure SYKE for your server: verification, welcomes, awards and what it reads.",
        ["verify", "welcome", "awards", "track", "untrack", "channels", "prefix", "timezone",
         "rescan", "collect", "dashboard", "demo", "sample"],
        {
            "verify": "A one-click verification gate. `verify setup` walks through the role, channel, button and "
                      "wording; members run `verify` (or tap the button) to get in.",
            "welcome": "Custom welcome messages for new members — title, text, image, colour, pings and links to your "
                       "rules/roles channels. `welcome channel #c` turns it on; `welcome test` previews it.",
            "awards": "Weekly server awards (funniest, most toxic, etc.), posted automatically. "
                      "`awards channel #c` turns them on; `awards now` posts immediately.",
            "channels": "Shows SYKE's current settings and every channel it's reading.",
            "prefix": "Set a custom command prefix, switch to @mention-only, or reset to the default.",
            "dashboard": "Opens the in-Discord admin control panel with one-click buttons for everything.",
            "collect": "Reads a channel's history so `mimic` and `yap` have more material to work with.",
        },
    ),
    Category(
        "meta", "ℹ️", "About & Account", EVERYONE,
        "Info about SYKE, adding it elsewhere, and controlling your own data.",
        ["help", "about", "invite", "inviter", "showcase", "diagnose", "scanme", "optout", "optin", "servers"],
        {
            "help": "This menu. `help <command>` explains any single command in detail.",
            "showcase": "A guided tour of every feature, with examples.",
            "scanme": "Digs up your older messages so they count toward your profile.",
            "optout": "Stops SYKE reading or judging your messages here and deletes what it has. `optin` re-enables it.",
            "diagnose": "Checks why SYKE's prefix commands might not be working in this channel.",
            "servers": "SYKE owners only — lists every server using SYKE.",
        },
    ),
]

CATEGORY_BY_KEY = {c.key: c for c in CATALOG}
# command name -> category, for quick lookup and the coverage test.
CATEGORY_OF: dict[str, Category] = {name: c for c in CATALOG for name in c.commands}


def _sig(cmd: commands.Command) -> str:
    """A compact usage signature, preferring the command's declared `usage`."""
    usage = getattr(cmd, "usage", None)
    if usage:
        return usage
    return cmd.signature or ""


def _short(cmd: commands.Command, cat: Category) -> str:
    """Best one-line description for a command."""
    curated = cat.details.get(cmd.name)
    if curated:
        # First sentence of the curated text keeps the list tidy.
        first = curated.split(". ")[0].rstrip(".")
        return first
    return (cmd.description or cmd.short_doc or "").strip() or "—"


def _line(cmd: commands.Command, cat: Category, prefix: str) -> str:
    # One tight line per command: `!name <args>` — short description.
    sig = _sig(cmd)
    head = f"`{prefix}{cmd.qualified_name}{(' ' + sig) if sig else ''}`"
    return f"{head} — {_short(cmd, cat)}"


def category_embed(bot, cat: Category, prefix: str) -> discord.Embed:
    lines = []
    for name in cat.commands:
        cmd = bot.get_command(name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        lines.append(_line(cmd, cat, prefix))
    e = embed(f"{cat.emoji} {cat.title}", f"{cat.blurb}\n\n" + "\n".join(lines))
    e.set_footer(text=f"{cat.access}  •  {prefix}help <command> for full details")
    return e


def overview_embed(bot, prefix: str, version: str) -> discord.Embed:
    # Compact: one short line per category. The dropdown drills into each.
    lines = [f"{c.emoji} **{c.title}** — {c.blurb}" for c in CATALOG]
    body = (f"Pick a category below, or `{prefix}help <command>` for one command's details. "
            f"Prefix: `{prefix}` (slash works too).\n\n" + "\n".join(lines))
    e = embed("🧠 SYKE — Command Menu", body)
    total = sum(len(c.commands) for c in CATALOG)
    e.set_footer(text=f"SYKE {version}  •  {total} commands in {len(CATALOG)} categories")
    return e


def command_embed(bot, cmd: commands.Command, prefix: str) -> discord.Embed:
    cat = CATEGORY_OF.get(cmd.qualified_name.split()[0])
    detail = (cat.details.get(cmd.name) if cat else None) or cmd.description or cmd.short_doc or "No description yet."
    e = embed(f"{(cat.emoji + ' ') if cat else ''}{prefix}{cmd.qualified_name}", detail)
    sig = _sig(cmd)
    e.add_field(name="Usage", value=f"`{prefix}{cmd.qualified_name}{(' ' + sig) if sig else ''}`", inline=False)
    if getattr(cmd, "aliases", None):
        e.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)
    if isinstance(cmd, commands.Group):
        subs = [c for c in cmd.commands if not getattr(c, "hidden", False)]
        if subs:
            lines = []
            for sub in sorted(subs, key=lambda c: c.name):
                desc = (sub.description or sub.short_doc or "").strip()
                lines.append(f"`{prefix}{cmd.qualified_name} {sub.name}` — {desc}" if desc
                             else f"`{prefix}{cmd.qualified_name} {sub.name}`")
            e.add_field(name="Sub-commands", value="\n".join(lines)[:1024], inline=False)
    if cat:
        e.set_footer(text=f"Who can use it: {cat.access}  •  {cat.emoji} {cat.title}")
    return e


class HelpView(discord.ui.View):
    """A dropdown that swaps between the overview and each category."""

    def __init__(self, bot, prefix: str, version: str, author_id: int, *, timeout: float = 150):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.prefix = prefix
        self.version = version
        self.author_id = author_id
        self.message: discord.Message | None = None

        options = [discord.SelectOption(label="Menu overview", value="__home__",
                                        description="All categories at a glance", emoji="🏠")]
        for cat in CATALOG:
            options.append(discord.SelectOption(
                label=cat.title, value=cat.key, emoji=cat.emoji,
                description=cat.blurb[:100]))
        self.select = discord.ui.Select(placeholder="Browse a category…", options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours — run the help command yourself to drive it.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        choice = self.select.values[0]
        if choice == "__home__":
            e = overview_embed(self.bot, self.prefix, self.version)
        else:
            e = category_embed(self.bot, CATEGORY_BY_KEY[choice], self.prefix)
        await interaction.response.edit_message(embed=e, view=self)

    async def on_timeout(self) -> None:
        self.select.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


def resolve_command(bot, query: str) -> commands.Command | None:
    """Find a command by name, qualified name, or alias (groups included)."""
    query = query.strip().lstrip("/").lower()
    cmd = bot.get_command(query)
    if cmd is not None:
        return cmd
    # Try matching a bare sub-command name across groups.
    for c in bot.walk_commands():
        if c.name == query or query in getattr(c, "aliases", ()):
            return c
    return None
