"""Welcome messages, sent when someone gets verified, with a gentle roast of their name and avatar."""

from __future__ import annotations

import random
import re

import discord

BRAND = discord.Colour.from_str("#8B5CF6")
CONFIG_KEY = "welcome"
DEFAULT_TITLE = "Welcome to {server}, {name} 👋"
DEFAULT_TEXT = (
    "{roast}\n\n"
    "You're member **#{count}**. Read the rules, touch grass occasionally, and enjoy the chaos.{next_steps}"
)
PLACEHOLDERS = {
    "mention": "pings them",
    "name": "their display name",
    "username": "their username",
    "server": "the server name",
    "count": "how many members the server has",
    "roast": "SYKE's roast of their name and avatar",
    "roles": "the roles channel",
    "colors": "the colour roles channel",
    "next_steps": "a sentence pointing to the roles and colour channels",
}
CHANNEL_TOKENS = ("{next_steps}", "{roles}", "{colors}")
TITLE_LIMIT = 256
TEXT_LIMIT = 3000
HEX = re.compile(r"#?([0-9a-fA-F]{6})")
URL = re.compile(r"https?://\S+\.\S+")

GENERIC = (
    "{name} just walked in like they own the place. They do not.",
    "Everyone act normal, {name} is here. Too late, they saw everything.",
    "{name} has entered the lab. Specimen looks harmless. For now.",
    "A wild {name} appeared. SYKE has already started taking notes.",
    "{name} finally made it. The bar was on the floor and they still tripped over it.",
    "Welcome {name}. Your first message will be judged. Harshly. Lovingly. Mostly harshly.",
)
DEFAULT_AVATAR = (
    "{name} is rocking the default avatar. Mysterious, or still looking for the settings button?",
    "No profile picture, {name}? Witness protection or just vibes?",
)
NUMBERS = (
    "{name} has {digits} numbers in their username, so all the good ones were taken. Iconic.",
    "The numbers in '{username}' tell us {name} arrived fashionably late to Discord.",
)
GAMER = (
    "'{username}' has strong 2009 gamer tag energy. Respect the commitment.",
    "{name}'s username was clearly crafted in a lobby at 3am. We're honoured.",
)
LONG = (
    "{name}'s name is so long it needs its own channel.",
)
SHOUTY = (
    "{name} even TYPES THEIR NAME IN CAPS. We're in for a loud one.",
)


def offline_roast(display_name: str, username: str, has_avatar: bool, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    digits = sum(ch.isdigit() for ch in username)
    pools = []
    if not has_avatar:
        pools.append(DEFAULT_AVATAR)
    if digits >= 2:
        pools.append(NUMBERS)
    if re.search(r"(xx|_{2,}|^_|_$|69|420|1337|pro|gamer|god|king|sigma)", username.lower()):
        pools.append(GAMER)
    if len(display_name) >= 18:
        pools.append(LONG)
    letters = [ch for ch in display_name if ch.isalpha()]
    if len(letters) >= 3 and all(ch.isupper() for ch in letters):
        pools.append(SHOUTY)
    pool = rng.choice(pools) if pools and rng.random() < 0.8 else GENERIC
    return rng.choice(pool).format(name=display_name, username=username, digits=digits)


def has_custom_avatar(member) -> bool:
    return getattr(member, "avatar", None) is not None or getattr(member, "guild_avatar", None) is not None


def avatar_for_ai(member) -> str | None:
    if not has_custom_avatar(member):
        return None
    avatar = getattr(member, "display_avatar", None)
    if avatar is None:
        return None
    try:
        return avatar.replace(format="png", size=256).url
    except (AttributeError, ValueError, TypeError):
        return getattr(avatar, "url", None)


def fill(template: str, values: dict[str, str]) -> str:
    """Swap `{name}`-style placeholders. Plain replacement, so `{` in someone's text is harmless."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def parse_colour(text: str) -> int | None:
    match = HEX.fullmatch((text or "").strip())
    return int(match.group(1), 16) if match else None


def next_steps(roles: str, colors: str) -> str:
    """The sentence after "enjoy the chaos", fitted to whichever channels are set."""
    if roles and colors:
        return f" Grab your roles in {roles} and pick a name colour in {colors}."
    if roles:
        return f" Grab your roles in {roles}."
    if colors:
        return f" Pick a name colour in {colors}."
    return ""


def _channel_mention(guild, channel_id) -> str:
    if not channel_id:
        return ""
    getter = getattr(guild, "get_channel", None)
    channel = getter(channel_id) if getter else None
    return getattr(channel, "mention", None) or f"<#{channel_id}>"


def render(config: dict, member, guild, roast: str) -> tuple[str | None, discord.Embed]:
    roles = _channel_mention(guild, config.get("roles_channel"))
    colors = _channel_mention(guild, config.get("colors_channel"))
    steps = next_steps(roles, colors)
    values = {
        "mention": getattr(member, "mention", ""),
        "name": getattr(member, "display_name", None) or getattr(member, "name", "someone"),
        "username": getattr(member, "name", "") or "",
        "server": getattr(guild, "name", "the server"),
        "count": str(getattr(guild, "member_count", None) or "?"),
        "roast": roast,
        "roles": roles or "the roles channel",
        "colors": colors or "the colour roles channel",
        "next_steps": steps,
    }
    template = config.get("text") or DEFAULT_TEXT
    if steps and not any(token in template for token in CHANNEL_TOKENS):
        template = template.rstrip() + "\n\n" + steps.strip()  # Custom text without the placeholders still gets them.
    title = fill(config.get("title") or DEFAULT_TITLE, values)[:TITLE_LIMIT]
    text = fill(template, values)[:4096]
    colour = config.get("colour")
    embed = discord.Embed(title=title, description=text,
                          colour=discord.Colour(colour) if isinstance(colour, int) else BRAND)
    if config.get("thumbnail", True):
        avatar = getattr(getattr(member, "display_avatar", None), "url", None)
        if avatar:
            embed.set_thumbnail(url=avatar)
    if config.get("image"):
        embed.set_image(url=config["image"])
    if config.get("footer"):
        embed.set_footer(text=fill(config["footer"], values)[:2048])
    content = getattr(member, "mention", None) if config.get("ping", True) else None
    return content, embed


def describe(config: dict, guild, prefix: str) -> discord.Embed:
    channel = guild.get_channel(config["channel"]) if config.get("channel") else None
    where = channel.mention if channel else ("missing channel" if config.get("channel") else "off")
    colour = config.get("colour")
    lines = [
        f"**Channel:** {where}",
        f"**Title:** {config.get('title') or DEFAULT_TITLE}",
        f"**Message:** {(config.get('text') or DEFAULT_TEXT)[:600]}",
        f"**Roles channel:** {_channel_mention(guild, config.get('roles_channel')) or 'not set'} · "
        f"**Colour roles channel:** {_channel_mention(guild, config.get('colors_channel')) or 'not set'}",
        f"**Colour:** {('#%06X' % colour) if isinstance(colour, int) else 'SYKE purple'}",
        f"**Banner image:** {config.get('image') or 'none'}",
        f"**Footer:** {config.get('footer') or 'none'}",
        f"**Ping them:** {'yes' if config.get('ping', True) else 'no'} · "
        f"**Avatar thumbnail:** {'yes' if config.get('thumbnail', True) else 'no'}",
        "",
        "Sent when someone gets verified (`!verify`, the Verify button, or an admin verifying them).",
        "**Placeholders:** " + ", ".join(f"`{{{k}}}` {v}" for k, v in PLACEHOLDERS.items()),
        "",
        f"`{prefix}welcome channel #channel` · `{prefix}welcome title <text>` · `{prefix}welcome message <text>` · "
        f"`{prefix}welcome colour #ff8800` · `{prefix}welcome image <url|off>` · `{prefix}welcome footer <text|off>` · "
        f"`{prefix}welcome roles #channel|off` · `{prefix}welcome colors #channel|off` · "
        f"`{prefix}welcome ping on|off` · `{prefix}welcome test` · `{prefix}welcome off`",
        "Or edit it all at once with the **Welcome** page of `" + prefix + "dashboard`.",
    ]
    return discord.Embed(title="👋 Welcome message", description="\n".join(lines)[:4096], colour=BRAND)
