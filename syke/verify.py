"""Server verification: members run !verify, or an admin runs !verify @member."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

log = logging.getLogger("syke.verify")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
VERIFY_BUTTON = "syke:verify"
MAX_VERIFY_TEXT = 1000
RESET_WORDS = {"reset", "off", "clear", "default", "none"}


@dataclass(frozen=True)
class VerifySettings:
    role_id: int | None = None
    remove_role_id: int | None = None
    channel_id: int | None = None
    text: str | None = None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None

    @classmethod
    def load(cls, bot, guild_id: int) -> VerifySettings:
        row = bot.storage.verification(guild_id)
        return cls(*row)


def _embed(title: str, body: str, *, ok: bool = True) -> discord.Embed:
    return discord.Embed(title=title, description=body, colour=BRAND if ok else WARN)


def unacceptable_role(role, *, bot_position: int, other_id: int | None) -> str | None:
    """Why this role can't be given or taken, or None if it's fine."""
    is_default = getattr(role, "is_default", lambda: False)
    if is_default() if callable(is_default) else is_default:
        return f"**{role.name}** is everyone. Make a role members don't already have."
    if getattr(role, "managed", False):
        return f"**{role.name}** is managed by an integration or a Server Boost. SYKE can't hand that out."
    if other_id is not None and role.id == other_id:
        return "That's already the other verification role. Pick a different one."
    if getattr(role, "position", 0) >= bot_position:
        return f"Drag SYKE's role **above** {role.mention}, then try again."
    return None


def _has(member, role) -> bool:
    return any(getattr(existing, "id", None) == role.id for existing in getattr(member, "roles", ()))


def _channel(guild, channel_id: int | None):
    if channel_id is None:
        return None
    getter = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    return getter(channel_id) if getter else None


def _bot_position(guild) -> int | None:
    top = getattr(getattr(guild, "me", None), "top_role", None)
    position = getattr(top, "position", None)
    return position if isinstance(position, int) else None


def _can_manage_roles(guild) -> bool | None:
    perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if perms is None:
        return None
    return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_roles", False))


def explain(prefix: str, settings: VerifySettings, *, role: str, remove: str, channel: str, panel: str) -> discord.Embed:
    body = (
        "SYKE gives members a role once they verify.\n\n"
        "1. Make a role (for example **Verified**) and drag it **under** SYKE's role.\n"
        "2. Hide the rest of the server from @everyone, and let that role see it.\n"
        f"3. `{prefix}verify role @Verified`\n"
        f"4. `{prefix}verify channel #gate` if they should verify in one channel only.\n"
        f"5. `{prefix}verify panel` to post the button.\n\n"
        f"Members run `{prefix}verify` or press **Verify**. "
        f"An admin can run `{prefix}verify @member` for them, "
        f"or `{prefix}verify @member @Role` to grant a role of their choosing. "
        f"`{prefix}verify take @Unverified` also removes a role when they pass. "
        f"`{prefix}verify message <text>` changes the panel (`reset` restores it). "
        f"`{prefix}verify off` turns the gate off.\n\n"
        f"**Role:** {role}\n"
        f"**Take away:** {remove}\n"
        f"**Channel:** {channel}\n"
        f"**Panel:** {panel}"
    )
    title = "Verification gate 🚪" if settings.role_id else "Set up verification 🚪"
    return _embed(title, body)


def describe(guild, settings: VerifySettings) -> dict[str, str]:
    role = _channel_role(guild, settings.role_id)
    remove = _channel_role(guild, settings.remove_role_id)
    channel = _channel(guild, settings.channel_id)
    return {
        "role": role.mention if role else ("missing role" if settings.role_id else "not set"),
        "remove": remove.mention if remove else ("missing role" if settings.remove_role_id else "nothing"),
        "channel": channel.mention if channel else ("missing channel" if settings.channel_id else "any channel"),
        "panel": "posted" if settings.panel_message_id else "not posted",
    }


def _channel_role(guild, role_id: int | None):
    if role_id is None:
        return None
    getter = getattr(guild, "get_role", None)
    return getter(role_id) if getter else None


def panel_embed(prefix: str, settings: VerifySettings) -> discord.Embed:
    body = settings.text or (
        f"Press **Verify**, or run `{prefix}verify`.\n"
        "SYKE gives you the role this server uses as a gate. No personality test. Yet."
    )
    embed = discord.Embed(title="Verify to enter", description=body, colour=BRAND)
    embed.set_footer(text="SYKE")
    return embed


def _names(guild, settings: VerifySettings) -> tuple[str, str, str, str]:
    labels = describe(guild, settings)
    return labels["role"], labels["remove"], labels["channel"], labels["panel"]


async def _apply_roles(member, grant_role, remove_role) -> str:
    added = False
    try:
        if not _has(member, grant_role):
            await member.add_roles(grant_role, reason="Verified with SYKE")
            added = True
        if remove_role is not None and _has(member, remove_role):
            await member.remove_roles(remove_role, reason="Verified with SYKE")
    except discord.Forbidden:
        return "partial" if added or _has(member, grant_role) else "forbidden"
    except discord.HTTPException:
        log.warning("Verification role change failed for %s", getattr(member, "id", "?"), exc_info=True)
        return "error"
    return "ok"


def staff_only(prefix: str, *, other: bool) -> discord.Embed:
    if other:
        return _embed(
            "Admins only",
            "Only someone with **Manage Server** can verify another person.",
            ok=False,
        )
    return _embed(
        "Admins only",
        f"Only someone with **Manage Server** can choose the role. `{prefix}verify` uses the one already set.",
        ok=False,
    )


def bots_stay_out() -> discord.Embed:
    return _embed("Bots stay out", "SYKE doesn't verify bots.", ok=False)


def _person(member, *, for_self: bool) -> str:
    if for_self:
        return getattr(member, "display_name", None) or "You"
    return getattr(member, "mention", None) or getattr(member, "display_name", None) or "They"


async def grant(
    bot,
    guild,
    member,
    channel_id: int | None,
    *,
    prefix: str,
    admin: bool,
    chosen_role=None,
    ignore_channel: bool = False,
    for_self: bool = True,
) -> discord.Embed:
    """Give the member the verified role, or explain why not.

    `chosen_role` grants that role instead of the one saved with `!verify role`.
    `ignore_channel` skips the gate, used when an admin verifies someone else.
    """
    settings = VerifySettings.load(bot, guild.id)
    role_label, remove_label, channel_label, panel_label = _names(guild, settings)
    role = chosen_role
    if role is None:
        if settings.role_id is None:
            if admin:
                return explain(prefix, settings, role=role_label, remove=remove_label, channel=channel_label, panel=panel_label)
            return _embed("Verification isn't on", "An admin hasn't set up a verification role here.", ok=False)
        role = _channel_role(guild, settings.role_id)
        if role is None:
            return _embed(
                "Verification is broken",
                f"The verified role is gone. An admin needs to run `{prefix}verify role` again.",
                ok=False,
            )
    if _can_manage_roles(guild) is False:
        return _embed(
            "I can't hand out roles",
            "Give SYKE **Manage Roles**, and keep its role above the verified one.",
            ok=False,
        )
    position = _bot_position(guild)
    if position is None:
        return _embed("Not ready", "SYKE can't see its own role yet. Try again in a moment.", ok=False)
    other_id = settings.remove_role_id if chosen_role is not None else None
    problem = unacceptable_role(role, bot_position=position, other_id=other_id)
    if problem:
        return _embed("Role won't work", problem, ok=False)

    remove = _channel_role(guild, settings.remove_role_id)
    remove_blocked = remove is not None and unacceptable_role(remove, bot_position=position, other_id=None) is not None
    held_back = None if remove_blocked else remove
    needs_grant = not _has(member, role)
    needs_remove = held_back is not None and _has(member, held_back)
    if not needs_grant and not needs_remove:
        if remove_blocked and remove is not None and _has(member, remove):
            if for_self:
                return _embed(
                    "You're in, almost",
                    f"You have {role.mention}, but SYKE can't remove {remove.mention}. Drag SYKE's role above it.",
                )
            return _embed(
                "Verified, almost",
                f"{_person(member, for_self=False)} has {role.mention}, but SYKE can't remove {remove.mention}. "
                "Drag SYKE's role above it.",
            )
        if for_self:
            return _embed("Already verified", "You're already in. SYKE noticed.")
        return _embed("Already verified", f"{_person(member, for_self=False)} already has {role.mention}.")
    gate = _channel(guild, settings.channel_id)
    if not ignore_channel and settings.channel_id and gate is not None and channel_id != settings.channel_id:
        return _embed("Not here", f"Verify in {gate.mention}.", ok=False)

    had_remove = needs_remove
    result = await _apply_roles(member, role, held_back)
    if result == "forbidden":
        return _embed(
            "I can't hand out roles",
            f"Give SYKE **Manage Roles**, and drag its role above {role.mention}.",
            ok=False,
        )
    if result == "error":
        return _embed("Discord refused", "The role change didn't go through. Try again in a moment.", ok=False)
    if result == "partial" or (remove_blocked and remove is not None and _has(member, remove)):
        stuck = remove.mention if remove is not None else "the other role"
        title = "You're in, almost" if for_self else "Verified, almost"
        return _embed(
            title,
            f"{_person(member, for_self=for_self)} has {role.mention}, but SYKE couldn't remove {stuck}. "
            "Drag SYKE's role above it.",
        )
    log.info("%s verified in %s", getattr(member, "id", "?"), guild.id)
    if for_self:
        who = _person(member, for_self=True)
        return _embed("You're in ✅", f"{who} now has {role.mention}. The lab is open.")
    who = _person(member, for_self=False)
    if had_remove and remove is not None:
        body = f"{who} now has {role.mention}, and {remove.mention} is gone."
    else:
        body = f"{who} now has {role.mention}."
    if settings.remove_role_id is None:
        body += f" Nothing is set to take away. `{prefix}verify take @Unverified` removes a role too."
    elif remove is None:
        body += f" The role to take away is gone. Set it again with `{prefix}verify take`."
    return _embed("Verified ✅", body)


def _save_role(bot, guild, role, *, kind: str, prefix: str) -> discord.Embed:
    settings = VerifySettings.load(bot, guild.id)
    position = _bot_position(guild)
    if position is None:
        return _embed("Not ready", "SYKE can't see its own role yet. Try again in a moment.", ok=False)
    if _can_manage_roles(guild) is False:
        return _embed("I can't hand out roles", "Give SYKE **Manage Roles** first.", ok=False)
    other = settings.remove_role_id if kind == "grant" else settings.role_id
    problem = unacceptable_role(role, bot_position=position, other_id=other)
    if problem:
        return _embed("That role won't work", problem, ok=False)
    field = "role_id" if kind == "grant" else "remove_role_id"
    bot.storage.update_verification(guild.id, **{field: role.id})
    if kind == "grant":
        return _embed(
            "Verified role set ✅",
            f"Members who verify get {role.mention}. Hide your other channels from @everyone, "
            f"let this role see them, then run `{prefix}verify panel`.",
        )
    return _embed("Role to take away ✅", f"Verifying removes {role.mention} as well as granting the member role.")


def set_grant_role(bot, guild, role, *, prefix: str) -> discord.Embed:
    return _save_role(bot, guild, role, kind="grant", prefix=prefix)


def set_remove_role(bot, guild, role, *, prefix: str) -> discord.Embed:
    if role is None:
        bot.storage.update_verification(guild.id, remove_role_id=None)
        return _embed("Not taking a role away", "Verifying only grants the member role now.")
    return _save_role(bot, guild, role, kind="remove", prefix=prefix)


def set_channel(bot, guild, channel, *, prefix: str) -> discord.Embed:
    settings = VerifySettings.load(bot, guild.id)
    if settings.role_id is None:
        role, remove, gate, panel = _names(guild, settings)
        return explain(prefix, settings, role=role, remove=remove, channel=gate, panel=panel)
    bot.storage.update_verification(guild.id, channel_id=channel.id)
    return _embed(
        "Verification channel set ✅",
        f"People can only verify in {channel.mention}. Post the button there with `{prefix}verify panel`.",
    )


def clear_channel(bot, guild) -> discord.Embed:
    bot.storage.update_verification(guild.id, channel_id=None)
    return _embed("Any channel ✅", "People can verify wherever they can talk to SYKE.")


def set_text(bot, guild, text: str, *, prefix: str) -> discord.Embed:
    cleaned = text.strip()
    if not cleaned or cleaned.lower() in RESET_WORDS:
        bot.storage.update_verification(guild.id, text=None)
        return _embed("Default wording ✅", f"The panel is back to SYKE's line. Refresh it with `{prefix}verify panel`.")
    if len(cleaned) > MAX_VERIFY_TEXT:
        return _embed("Too long", f"Keep the panel under {MAX_VERIFY_TEXT} characters.", ok=False)
    bot.storage.update_verification(guild.id, text=cleaned)
    return _embed("Panel wording saved ✅", f"Refresh the posted button with `{prefix}verify panel` if it doesn't update.")


def turn_off(bot, guild) -> discord.Embed:
    removed = bot.storage.clear_verification(guild.id)
    if not removed:
        return _embed("Already off", "Verification wasn't set up.")
    return _embed("Verification off", "The gate is gone. An old button will say it isn't set up.")


def explain_current(bot, guild, prefix: str) -> discord.Embed:
    settings = VerifySettings.load(bot, guild.id)
    role, remove, channel, panel = _names(guild, settings)
    return explain(prefix, settings, role=role, remove=remove, channel=channel, panel=panel)


def _can_post(guild, channel) -> bool:
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    perms = channel.permissions_for(me)
    return bool(perms.send_messages and perms.embed_links)


async def _edit_panel(channel, message_id: int, embed: discord.Embed) -> bool:
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=VerifyView())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False
    return True


async def refresh_panel(bot, guild, prefix: str) -> bool:
    settings = VerifySettings.load(bot, guild.id)
    channel = _channel(guild, settings.panel_channel_id)
    if channel is None or not settings.panel_message_id:
        return False
    return await _edit_panel(channel, settings.panel_message_id, panel_embed(prefix, settings))


async def post_panel(bot, guild, here, prefix: str) -> discord.Embed:
    settings = VerifySettings.load(bot, guild.id)
    if settings.role_id is None:
        return explain_current(bot, guild, prefix)
    destination = _channel(guild, settings.channel_id) if settings.channel_id else here
    if settings.channel_id and destination is None:
        return _embed(
            "Channel is gone",
            f"The verification channel was deleted. Set a new one with `{prefix}verify channel`, "
            f"or `{prefix}verify anywhere` to allow every channel.",
            ok=False,
        )
    if not _can_post(guild, destination):
        return _embed(
            "I can't post there",
            f"Give SYKE **Send Messages** and **Embed Links** in {destination.mention}.",
            ok=False,
        )
    embed = panel_embed(prefix, VerifySettings.load(bot, guild.id))
    if settings.panel_channel_id == destination.id and settings.panel_message_id:
        if await _edit_panel(destination, settings.panel_message_id, embed):
            return _embed("Panel updated ✅", f"The button in {destination.mention} shows the current wording.")
    old = _channel(guild, settings.panel_channel_id)
    if old is not None and settings.panel_message_id and getattr(old, "id", None) != destination.id:
        try:
            stale = await old.fetch_message(settings.panel_message_id)
            await stale.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        message = await destination.send(embed=embed, view=VerifyView())
    except discord.Forbidden:
        return _embed(
            "I can't post there",
            f"Give SYKE **Send Messages** and **Embed Links** in {destination.mention}.",
            ok=False,
        )
    except discord.HTTPException:
        log.warning("Couldn't post the verification panel in %s", getattr(destination, "id", "?"), exc_info=True)
        return _embed("Discord refused", "The panel didn't post. Try again in a moment.", ok=False)
    bot.storage.update_verification(guild.id, panel_channel_id=destination.id, panel_message_id=message.id)
    return _embed("Panel posted ✅", f"The Verify button is in {destination.mention}. It keeps working after a restart.")


class VerifyView(discord.ui.View):
    """Persistent button, so a posted panel still verifies people after a restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id=VERIFY_BUTTON)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=_embed("Servers only", "Verification happens in a server.", ok=False),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not hasattr(member, "add_roles"):
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=_embed("Who?", "SYKE couldn't load your membership. Try again in a moment.", ok=False),
                    ephemeral=True,
                )
                return
        bot = interaction.client
        perms = getattr(member, "guild_permissions", None)
        owners = getattr(getattr(bot, "settings", None), "owner_ids", ())
        admin = member.id in owners or bool(perms and perms.manage_guild)
        prefix = bot.display_prefix(interaction.guild.id)
        embed = await grant(bot, interaction.guild, member, interaction.channel_id, prefix=prefix, admin=admin)
        await interaction.followup.send(embed=embed, ephemeral=True)
