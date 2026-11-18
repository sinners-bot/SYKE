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
    # Optional grouping for the category page: [(heading, [command, ...]), ...].
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    # Commands whose access differs from the category's (e.g. an admin command among everyone's).
    access_of: dict[str, str] = field(default_factory=dict)


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
        ["guess", "drunk", "high", "scramble", "zalgo", "uwu", "mock", "keysmash",
         "leet", "stutter", "clap", "reverse", "woke", "afflict", "cure", "afflicted"],
        {
            "guess": "\"Who said this?\" — SYKE quotes a real message and you guess the author. "
                     "`guess top` shows the best guessers.",
            "drunk": "Slurs the text like they've had a few — doubled letters, dropped letters, the odd *hic*.",
            "scramble": "Shuffles the middle of each word but keeps the first and last letter — still readable, barely.",
            "zalgo": "Corrupts the text into c̸u̷r̶s̵e̴d̷ glitch text.",
            "uwu": "Twanslates the text into uwu-speak, wiw faces and aww.",
            "mock": "aLtErNaTiNg mOcKiNg CaPs, SpongeBob style.",
            "keysmash": "Letters collapse into keyboard mash the further along they go.",
            "high": "Spaced-out stoner talk — stretched vowels, filler words, trailing off mid-thought.",
            "leet": "h4ck3r l33tsp34k — swaps letters for lookalike numbers.",
            "stutter": "n-nervous repeated first letters.",
            "clap": "Puts 👏 a 👏 clap 👏 between 👏 every 👏 word.",
            "reverse": "Reverses the whole message, sdrawkcab.",
            "woke": "AI-rewrites the message to be kind and inclusive — slurs become friendly words, hate flips "
                    "to love (e.g. \"I hate X\" → \"I love X\"). Falls back to an affirming slogan without AI.",
            "afflict": "Stick an effect on someone (Manage Messages): every message they send is rewritten in that "
                       "effect, reposted as them via webhook. Effects: drunk, high, scramble, zalgo, uwu, mock, "
                       "keysmash, leet, stutter, clap, reverse, woke. `afflict @user woke`.",
            "cure": "Remove a user's active affliction and let them type normally again.",
            "afflicted": "List everyone who currently has a chat effect stuck on them.",
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
        "economy", "💸", "The Strip", EVERYONE,
        "A neon Vegas economy: earn, shop, scheme and own a storefront.",
        ["daily", "work", "hustle", "pay", "balance", "deposit", "withdraw", "rich",
         "shop", "buy", "use", "inventory", "pawn", "rob", "crime", "heist", "jail", "bail",
         "property", "cashup", "thestrip", "stripfeed", "economy", "house"],
        {
            "balance": "Your Players Club card — cash, chips, bank, net worth and tier (Bronze up to Black). "
                       "@mention someone to see theirs.",
            "daily": "Claim your daily cash — claim on consecutive days to build a streak for bigger payouts.",
            "work": "Clock a themed shift on the Strip for cash. Hourly cooldown. Food and a fresh set of nails "
                    "boost your pay.",
            "hustle": "A risky earn: usually pays, sometimes flops, occasionally backfires.",
            "pay": "Send cash to another member. A small tax is skimmed off the top.",
            "deposit": "Bank your cash. Money in the bank can't be robbed — cash on hand can.",
            "rich": "The leaderboard — richest net worth on the Strip.",
            "shop": "Every storefront sells something: scratchers and padlocks at the General Store, food buffs at "
                    "the Golden Dragon, luck from Madame Rose, rare vinyl at Dead Air Records, a custom name colour "
                    "at Sherwin Williams. `shop <store>` looks inside one.",
            "buy": "Buy an item with cash: `buy padlock`, `buy scratch 5`. The store's owner gets 10% of the sale.",
            "use": "Use an item: `use scratch`, `use crate`, `use lomein`. Paint jobs take a colour: "
                   "`use paintjob #ff3ea5`.",
            "pawn": "Sell items back at Pawn Deals for 40% (records for 80% of their value). `pawn crate all`.",
            "rob": "Lift 10–30% of someone's cash on hand (the bank is safe). Padlocks stop you cold. Get caught and "
                   "you pay them a fine and go to jail for 30 minutes.",
            "crime": "A solo job: 400–1,200 cash if it works, a fine and 20–45 minutes in jail if it doesn't.",
            "heist": "Hit an owned storefront's register for half of what's in it. Trip the alarm and you pay the "
                     "owner damages and do 45 minutes.",
            "jail": "See who's locked up. Jailed players can't earn or gamble until they're out.",
            "bail": "Post bail at Bail Bonds — for yourself or a friend. Costs more the longer the sentence left.",
            "property": "11 storefronts per server, one owner each. Buy one and it earns every hour into its register "
                        "(holds a day's takings), plus 10% of every sale inside. `property buy/upgrade/sell`.",
            "cashup": "Empty the registers of every storefront you own — before someone heists them.",
            "thestrip": "Post THE STRIP game with a Play button and how many people are on it right now. "
                        "Also `/strip play`.",
            "stripfeed": "Admins: `stripfeed #channel` posts highlights from this server's players (arcade records, big "
                         "casino wins, weddings, cup champions, promotions, new arrivals) with a Play button. "
                         "`stripfeed off` stops it. Also `/strip feed`.",
            "economy": "Admins: `economy on`/`off` opens or closes the Strip, plus `give`/`take`/`reset` wallets.",
            "house": "SYKE owners: run the economy in any server — `give`/`take`/`set`/`peek`/`reset` wallets, "
                     "`open`/`close`/`wipe` servers, `ban` (86) players, `jail`/`free`, `happyhour <x> [hours]` "
                     "and `maintenance on/off` for the casino.",
        },
        sections=[("Earn", ["daily", "work", "hustle", "pay"]),
                  ("Wallet", ["balance", "deposit", "withdraw", "rich"]),
                  ("Shops", ["shop", "buy", "use", "inventory", "pawn"]),
                  ("Crime", ["rob", "crime", "heist", "jail", "bail"]),
                  ("Property", ["property", "cashup"]),
                  ("The game", ["thestrip", "stripfeed"]),
                  ("Admin", ["economy", "house"])],
        access_of={"economy": ADMIN, "house": OWNER, "stripfeed": ADMIN},
    ),
    Category(
        "love", "💞", "Love", EVERYONE,
        "Marry, divorce and send gifts. Shared with THE STRIP game: married here means married there.",
        ["marry", "divorce", "partner", "gift"],
        {
            "marry": "Propose: `marry @someone [ring]`. Nothing happens until they press Yes, then you're married here "
                     "and in THE STRIP game. Rings: `paper` (free bottle-cap), `promise`, `gold`, `diamond`, paid from "
                     "your cash only if they say yes. A ring you already own in the game is used instead.",
            "divorce": "End your marriage, engagement or relationship. One-sided, like on the Strip: only you decide. "
                       "Asks you to confirm.",
            "partner": "Who you (or @someone) are with: married since, the ring, dates and time together in the game. "
                       "Respects the game's privacy setting.",
            "gift": "`gift @someone roses`: chocolates, roses, a teddy, perfume or a gold watch, paid from your cash, "
                    "and a free wildflower once a day. It lands in their pocket in the game. `shop chapel` lists them.",
        },
    ),
    Category(
        "casino", "🎰", "Casino", EVERYONE,
        "Real Vegas tables. Bets top up from your cash automatically.",
        ["blackjack", "roulette", "slots", "hilo", "flip", "chips", "cashout"],
        {
            "blackjack": "A real Vegas Strip table: 6-deck shoe, blackjack pays 3:2, dealer stands on all 17s. "
                         "Hit, stand, double down, split any pair (10s included, up to 4 hands), insurance / "
                         "even money and late surrender — all with buttons.",
            "roulette": "Bet a number (0-36) or red/black/even/odd/low/high on a single-zero wheel.",
            "slots": "Pull the neon slots. Three-of-a-kind pays big; two cherries pays small.",
            "hilo": "Higher or lower than the current card? Every right call grows your multiplier — cash out "
                    "before you miss. Aces high, ties lose.",
            "flip": "Coinflip for chips — call `heads` or `tails`. Pays 1.95×.",
            "chips": "The cash cage — `chips 500` swaps cash for chips, free and 1:1. You rarely need it: "
                     "any bet tops itself up from your cash automatically.",
            "cashout": "Swap chips back into cash at the cage. Free, 1:1. Defaults to all your chips.",
        },
        sections=[("Tables", ["blackjack", "roulette", "slots", "hilo", "flip"]),
                  ("Cash cage", ["chips", "cashout"])],
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

# Compact titles, one-line blurbs and sections for the menu (detailed text above stays for `help <cmd>`).
LAYOUT: dict[str, tuple[str, str, list[tuple[str, list[str]]]]] = {
    "profile": ("Profile", "SYKE's read on people, from what they post.", []),
    "ai": ("AI", "Talk to SYKE — or let it join the chat.", []),
    "games": ("Fun", "Games and text toys. Reply to a message or pass text.", [
        ("Games", ["guess"]),
        ("Text effects", ["drunk", "high", "scramble", "zalgo", "uwu", "mock", "keysmash", "leet", "stutter",
                          "clap", "reverse", "woke"]),
        ("Afflictions", ["afflict", "cure", "afflicted"])]),
    "emoji": ("Emojis", "Grow your emoji and sticker collection.", []),
    "mod": ("Moderation", "Every action is logged as a numbered case.", [
        ("Warnings", ["warn", "warnings", "history", "delwarn", "clearwarns"]),
        ("Timeouts & bans", ["timeout", "untimeout", "kick", "ban", "unban", "tempban", "softban"]),
        ("Channels", ["purge", "slowmode", "lock", "unlock"]),
        ("Restrictions", ["restrict", "unrestrict", "restrictions", "modlog"])]),
    "automod": ("Automod", "Rules enforced on every message. Mods are never caught.", []),
    "security": ("Security", "Anti-nuke and anti-raid. Off until you turn it on.", []),
    "setup": ("Setup", "Verification, welcomes and what SYKE reads.", [
        ("Members", ["verify", "welcome", "awards"]),
        ("Reading", ["track", "untrack", "channels", "rescan", "collect"]),
        ("Server", ["prefix", "timezone", "dashboard", "demo", "sample"])]),
    "meta": ("About", "SYKE itself, and your data.", [
        ("SYKE", ["help", "about", "invite", "showcase", "diagnose", "inviter"]),
        ("Your data", ["scanme", "optout", "optin"]),
        ("Owners", ["servers"])]),
}
# Sections rendered as a single line of command names (they all work the same way).
INLINE_SECTIONS = {"Text effects"}
ACCESS_OF = {"afflict": MODERATOR, "cure": MODERATOR, "afflicted": MODERATOR, "yap": ADMIN, "servers": OWNER,
             "verify": EVERYONE}

BRIEF = {
    "profile": "Full personality report", "iq": "IQ estimate", "aura": "Aura reading",
    "ship": "How well two people match", "top": "Trait leaderboard", "tldr": "Catch up on the chat",
    "mimic": "Talk like someone", "dexify": "Dramatic… pedro… pauses",
    "ask": "Ask SYKE anything", "yap": "Let SYKE chime into chats",
    "guess": "Who said it?", "afflict": "Stick an effect on someone", "cure": "Remove an effect",
    "afflicted": "Who's afflicted",
    "emoji": "Add, steal or manage emojis", "sticker": "Add, steal or manage stickers",
    "steal": "Steal from a replied message",
    "warn": "Warn a member", "warnings": "Someone's warnings", "history": "Full mod record",
    "delwarn": "Delete a warning", "clearwarns": "Clear all warnings",
    "timeout": "Time someone out", "untimeout": "End a timeout", "kick": "Kick a member",
    "ban": "Ban, even if they left", "unban": "Unban by ID", "tempban": "Ban for a set time",
    "softban": "Ban + unban to wipe messages",
    "purge": "Bulk-delete messages", "slowmode": "Set slowmode", "lock": "Lock a channel",
    "unlock": "Unlock a channel",
    "restrict": "Block images, links, reactions…", "unrestrict": "Lift restrictions",
    "restrictions": "Who's restricted", "modlog": "Set the mod-log channel",
    "automod": "Overview and settings", "filter": "Word and slur blacklist", "links": "Invite and link control",
    "antispam": "Floods, mentions and caps",
    "antinuke": "Stop rogue admins", "antiraid": "Stop join raids", "raidmode": "Lock the gate now",
    "verify": "Verify members", "welcome": "Welcome message", "awards": "Weekly chat awards",
    "track": "Let SYKE read a channel", "untrack": "Stop reading a channel",
    "channels": "Settings and watched channels", "rescan": "Re-read watched channels",
    "collect": "Read older history", "prefix": "Change the prefix", "timezone": "Server timezone",
    "dashboard": "Every setting, with buttons", "demo": "Add fake test members", "sample": "A demo member's report",
    "daily": "Daily cash and streak", "work": "Clock a shift", "hustle": "Risky quick cash", "pay": "Send cash",
    "balance": "Players Club card", "deposit": "Bank it (robber-proof)", "withdraw": "Take cash out",
    "rich": "Richest on the Strip",
    "shop": "Browse the storefronts", "buy": "Buy an item", "use": "Use an item", "inventory": "Items and buffs",
    "pawn": "Sell at Pawn Deals",
    "rob": "Rob someone's pocket cash", "crime": "Solo job: cash or jail", "heist": "Hit a store's register",
    "jail": "Who's locked up", "bail": "Post bail",
    "property": "Own a storefront", "cashup": "Empty your registers",
    "economy": "Open, close, manage wallets", "house": "Run the economy anywhere",
    "marry": "Propose (shared with the game)", "divorce": "End it, one-sided", "partner": "Who someone is with",
    "gift": "Send a gift",
    "blackjack": "Vegas rules, pays 3:2", "roulette": "European wheel", "slots": "Neon slots",
    "hilo": "Higher or lower", "flip": "Coinflip, pays 1.95×", "chips": "Cash → chips, free",
    "cashout": "Chips → cash, free",
    "help": "This menu", "about": "Bot stats", "invite": "Add SYKE to a server", "showcase": "Feature tour",
    "diagnose": "Fix prefix problems", "inviter": "Who invited someone", "scanme": "Count your older messages",
    "optout": "Stop SYKE reading you", "optin": "Opt back in", "servers": "Every server using SYKE",
}

for _cat in CATALOG:
    if _cat.key in LAYOUT:
        _cat.title, _cat.blurb, _sections = LAYOUT[_cat.key]
        if _sections and not _cat.sections:
            _cat.sections = _sections
    for _name, _level in ACCESS_OF.items():
        if _name in _cat.commands and _level != _cat.access:
            _cat.access_of.setdefault(_name, _level)

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
    """A few words for the compact menu."""
    if cmd.name in BRIEF:
        return BRIEF[cmd.name]
    text = (cmd.description or cmd.short_doc or "").strip() or "—"
    return text.split(" — ")[0].split(". ")[0][:44]


def _line(cmd: commands.Command, cat: Category, prefix: str) -> str:
    lock = " 🔒" if cmd.name in cat.access_of else ""
    return f"`{cmd.qualified_name}` {_short(cmd, cat)}{lock}"


def _live(bot, names: list[str]) -> list[commands.Command]:
    out = []
    for name in names:
        cmd = bot.get_command(name)
        if cmd is not None and not getattr(cmd, "hidden", False):
            out.append(cmd)
    return out


def category_embed(bot, cat: Category, prefix: str) -> discord.Embed:
    e = embed(f"{cat.emoji} {cat.title}", cat.blurb)
    sections = cat.sections or [("", cat.commands)]
    for heading, names in sections:
        cmds = _live(bot, names)
        if not cmds:
            continue
        if heading in INLINE_SECTIONS:
            value = " ".join(f"`{c.name}`" for c in cmds)
        else:
            value = "\n".join(_line(c, cat, prefix) for c in cmds)
        if heading:
            e.add_field(name=heading, value=value[:1024], inline=False)
        else:
            e.description = f"{cat.blurb}\n\n{value}"
    note = "  •  🔒 = " + " / ".join(sorted(set(cat.access_of.values()))) if cat.access_of else ""
    e.set_footer(text=f"{cat.access}{note}  •  {prefix}help <command> for usage")
    return e


def overview_embed(bot, prefix: str, version: str) -> discord.Embed:
    """A compact grid: each category with a few of its commands. The dropdown drills in."""
    e = embed("🧠 SYKE", f"Prefix `{prefix}` · slash works too · `{prefix}help <command>` for details\n"
                        "Pick a category below.")
    for cat in CATALOG:
        cmds = _live(bot, cat.commands)
        shown = " ".join(f"`{c.name}`" for c in cmds[:4]) + (" …" if len(cmds) > 4 else "")
        e.add_field(name=f"{cat.emoji} {cat.title} · {len(cmds)}", value=shown or "—", inline=True)
    while len(e.fields) % 3:  # keep the grid even
        e.add_field(name="\u200b", value="\u200b", inline=True)
    total = sum(len(_live(bot, c.commands)) for c in CATALOG)
    e.set_footer(text=f"SYKE {version}  •  {total} commands")
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
        who = cat.access_of.get(cmd.qualified_name.split()[0], cat.access)
        e.set_footer(text=f"Who can use it: {who}  •  {cat.emoji} {cat.title}")
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
