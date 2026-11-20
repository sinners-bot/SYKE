"""Role menus: members pick their own roles with buttons, a dropdown or reactions.

`rolemenu create <title>` posts an empty menu and gives it a number; `rolemenu add <number> @role 🎮`
fills it. Styles: buttons (default), dropdown, or reactions. An "exclusive" menu lets a member hold
only one of its roles at a time, which is what colour menus want; `rolemenu colors` builds a full
colour menu in one go.

Buttons and dropdowns are dynamic items (their custom ids carry the menu and role), so they keep
working after restarts without SYKE re-sending anything. SYKE only hands out roles below its own,
never roles with moderator permissions, and never roles above the person setting the menu up.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Literal, Optional

import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, OK, WARN, embed, moderator, problem
from .verify import unacceptable_role

log = logging.getLogger("syke.rolemenus")

CONFIG = "rolemenus"
MAX_ITEMS = 25
MAX_MENUS = 50
STYLES = ("buttons", "dropdown", "reactions")
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages", "ban_members",
             "kick_members", "moderate_members", "mention_everyone", "manage_webhooks", "manage_threads",
             "manage_expressions", "manage_events", "view_audit_log", "manage_nicknames")
COLOURS = [
    ("Red", "#EF4444", "🔴"), ("Orange", "#F97316", "🟠"), ("Yellow", "#FACC15", "🟡"), ("Green", "#22C55E", "🟢"),
    ("Mint", "#5EEAD4", "🩵"), ("Blue", "#3B82F6", "🔵"), ("Purple", "#8B5CF6", "🟣"), ("Pink", "#EC4899", "🩷"),
    ("Brown", "#A16207", "🟤"), ("White", "#F4F4F5", "⚪"), ("Black", "#18181B", "⚫"),
]


def role_issue(role, guild, author=None) -> str | None:
    me = getattr(guild, "me", None)
    position = getattr(getattr(me, "top_role", None), "position", None)
    if not isinstance(position, int):
        return "SYKE can't see its own role yet. Try again in a moment."
    issue = unacceptable_role(role, bot_position=position, other_id=None)
    if issue:
        return issue
    perms = getattr(role, "permissions", None)
    risky = [p for p in DANGEROUS if perms is not None and getattr(perms, p, False)]
    if risky:
        return f"**{role.name}** has moderator permissions ({risky[0].replace('_', ' ')}). Self-roles can't carry those."
    if author is not None and getattr(guild, "owner_id", None) != author.id:
        top = getattr(getattr(author, "top_role", None), "position", 0)
        if getattr(role, "position", 0) >= top:
            return f"**{role.name}** is at or above your own top role."
    return None


def parse_emoji(text: str | None) -> tuple[str | None, str | None]:
    """(stored emoji string or None, error)."""
    text = (text or "").strip()
    if not text:
        return None, None
    if re.fullmatch(r"<a?:\w+:\d+>", text):
        return text, None
    if emoji_lib.is_emoji(text):
        return text, None
    return None, f"**{text[:30]}** isn't an emoji."


def menu_embed(menu: dict) -> discord.Embed:
    lines = []
    for item in menu.get("items", []):
        icon = f"{item['emoji']} " if item.get("emoji") else ""
        label = f" · {item['label']}" if item.get("label") else ""
        lines.append(f"{icon}<@&{item['role']}>{label}")
    hint = {"buttons": "Press a button to get or drop a role.",
            "dropdown": "Pick from the menu below. Untick to drop a role.",
            "reactions": "React to get a role, remove your reaction to drop it."}[menu.get("style", "buttons")]
    if menu.get("exclusive"):
        hint += " You can hold one at a time."
    body = (menu.get("text") + "\n\n" if menu.get("text") else "") + ("\n".join(lines) or "*No roles yet.*")
    card = discord.Embed(title=menu.get("title") or "Pick your roles", description=body,
                         colour=discord.Colour.from_str(menu["colour"]) if menu.get("colour") else BRAND)
    card.set_footer(text=hint)
    return card


def item_label(item: dict, guild) -> str:
    if item.get("label"):
        return item["label"][:80]
    role = guild.get_role(item["role"]) if guild else None
    return (role.name if role else "Role")[:80]


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:rm:(?P<menu>\d+):(?P<role>\d+)"):
    def __init__(self, menu: int, role: int, label: str = "Role", emoji: str | None = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"syke:rm:{menu}:{role}"))
        self.menu, self.role = menu, role

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["menu"]), int(match["role"]), item.label or "Role", item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RoleMenus")
        if cog is None:
            await interaction.response.send_message("Role menus are offline right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await cog.toggle(interaction.guild, interaction.user, self.menu, self.role)
        await interaction.followup.send(text, ephemeral=True)


class RoleSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"syke:rms:(?P<menu>\d+)"):
    def __init__(self, menu: int, options: list[discord.SelectOption] | None = None, exclusive: bool = False) -> None:
        options = options or [discord.SelectOption(label="Role", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"syke:rms:{menu}", placeholder="Pick your roles",
                                           min_values=0, max_values=1 if exclusive else len(options), options=options))
        self.menu = menu

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        made = cls(int(match["menu"]), list(item.options) or None)
        made.item.max_values = item.max_values
        return made

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RoleMenus")
        if cog is None:
            await interaction.response.send_message("Role menus are offline right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        picked = {int(v) for v in self.item.values if v.isdigit()}
        text = await cog.set_roles(interaction.guild, interaction.user, self.menu, picked)
        await interaction.followup.send(text, ephemeral=True)


def menu_view(number: int, menu: dict, guild) -> discord.ui.View | None:
    items = menu.get("items", [])
    if not items or menu.get("style") == "reactions":
        return None
    view = discord.ui.View(timeout=None)
    if menu.get("style") == "dropdown":
        options = [discord.SelectOption(label=item_label(i, guild), value=str(i["role"]), emoji=i.get("emoji"))
                   for i in items]
        view.add_item(RoleSelect(number, options, bool(menu.get("exclusive"))))
    else:
        for i in items:
            view.add_item(RoleButton(number, i["role"], item_label(i, guild), i.get("emoji")))
    return view


class RoleMenus(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, RoleSelect)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton, RoleSelect)

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def data(self, guild_id: int) -> dict:
        data = self.bot.storage.get_config(guild_id, CONFIG)
        data.setdefault("menus", {})
        return data

    def save(self, guild_id: int, data: dict) -> None:
        self.bot.storage.set_config(guild_id, CONFIG, data)

    def menu(self, guild_id: int, number: int) -> dict | None:
        return self.data(guild_id)["menus"].get(str(number))

    # Giving roles

    async def toggle(self, guild, member, number: int, role_id: int) -> str:
        menu = self.menu(guild.id, number)
        if menu is None or role_id not in {i["role"] for i in menu.get("items", [])}:
            return "That menu option no longer exists."
        role = guild.get_role(role_id)
        if role is None:
            return "That role was deleted."
        if role_issue(role, guild):
            return "SYKE can't hand that role out any more. Ask an admin to fix the menu."
        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"Role menu #{number}")
                return f"Removed **{role.name}**."
            drop = [r for r in member.roles if menu.get("exclusive") and r.id != role.id
                    and r.id in {i["role"] for i in menu["items"]}]
            if drop:
                await member.remove_roles(*drop, reason=f"Role menu #{number} (one at a time)")
            await member.add_roles(role, reason=f"Role menu #{number}")
        except discord.HTTPException:
            return "Discord refused. SYKE needs **Manage Roles** and to sit above that role."
        swapped = f" (swapped from {', '.join(r.name for r in drop)})" if drop else ""
        return f"Gave you **{role.name}**{swapped}."

    async def set_roles(self, guild, member, number: int, picked: set[int]) -> str:
        menu = self.menu(guild.id, number)
        if menu is None:
            return "That menu no longer exists."
        allowed = {i["role"] for i in menu.get("items", [])}
        picked &= allowed
        if menu.get("exclusive") and len(picked) > 1:
            picked = {next(iter(picked))}
        have = {r.id for r in member.roles}
        add = [guild.get_role(r) for r in picked - have]
        remove = [guild.get_role(r) for r in (allowed & have) - picked]
        add = [r for r in add if r is not None and not role_issue(r, guild)]
        remove = [r for r in remove if r is not None and not role_issue(r, guild)]
        try:
            if remove:
                await member.remove_roles(*remove, reason=f"Role menu #{number}")
            if add:
                await member.add_roles(*add, reason=f"Role menu #{number}")
        except discord.HTTPException:
            return "Discord refused. SYKE needs **Manage Roles** and to sit above those roles."
        bits = []
        if add:
            bits.append("Added " + ", ".join(f"**{r.name}**" for r in add))
        if remove:
            bits.append("Removed " + ", ".join(f"**{r.name}**" for r in remove))
        return ". ".join(bits) + "." if bits else "No changes."

    def reaction_target(self, payload) -> tuple[int, dict, int] | None:
        if payload.guild_id is None:
            return None
        for key, menu in self.data(payload.guild_id)["menus"].items():
            if menu.get("message") != payload.message_id or menu.get("style") != "reactions":
                continue
            for item in menu.get("items", []):
                stored = item.get("emoji") or ""
                if payload.emoji.id:
                    matched = f":{payload.emoji.id}>" in stored
                else:
                    matched = stored == payload.emoji.name
                if matched:
                    return int(key), menu, item["role"]
        return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        me = getattr(self.bot, "user", None)
        if me is not None and payload.user_id == me.id:
            return
        found = self.reaction_target(payload)
        if found is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if guild is None or member is None or member.bot:
            return
        number, menu, role_id = found
        role = guild.get_role(role_id)
        if role is None or role_issue(role, guild) or role in member.roles:
            return
        with contextlib.suppress(discord.HTTPException):
            if menu.get("exclusive"):
                drop = [r for r in member.roles if r.id in {i["role"] for i in menu["items"]} and r.id != role_id]
                if drop:
                    await member.remove_roles(*drop, reason=f"Role menu #{number} (one at a time)")
            await member.add_roles(role, reason=f"Role menu #{number}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload) -> None:
        found = self.reaction_target(payload)
        if found is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if member is None:
            return
        number, _, role_id = found
        role = guild.get_role(role_id)
        if role is not None and role in member.roles and not role_issue(role, guild):
            with contextlib.suppress(discord.HTTPException):
                await member.remove_roles(role, reason=f"Role menu #{number}")

    # Posting

    async def refresh(self, guild, number: int, menu: dict) -> str | None:
        channel = guild.get_channel(menu.get("channel") or 0)
        if channel is None:
            return "The menu's channel is gone."
        message = channel.get_partial_message(menu["message"])
        try:
            await message.edit(embed=menu_embed(menu), view=menu_view(number, menu, guild))
        except discord.NotFound:
            return "The menu's message was deleted. Delete the menu and make a new one."
        except discord.HTTPException as error:
            return f"Discord refused the update: {error.text or error}"
        if menu.get("style") == "reactions":
            for item in menu.get("items", []):
                if item.get("emoji"):
                    with contextlib.suppress(discord.HTTPException):
                        await message.add_reaction(item["emoji"])
        return None

    # Commands

    @commands.hybrid_group(name="rolemenu", aliases=["rolemenus", "reactionroles", "rr", "selfroles"], fallback="list",
                           case_insensitive=True, description="Let members pick their own roles, colours and pings")
    @moderator("manage_roles")
    async def rolemenu(self, ctx: commands.Context) -> None:
        menus = self.data(ctx.guild.id)["menus"]
        p = self.p(ctx)
        usage = (f"`{p}rolemenu create <title>` then `{p}rolemenu add <number> @role 🎮 [label]`.\n"
                 f"`{p}rolemenu colors` makes a ready colour menu.\n"
                 f"Also: `{p}rolemenu remove`, `{p}rolemenu edit`, `{p}rolemenu delete`.")
        if not menus:
            await ctx.reply(embed=embed("🎭 No role menus yet", usage), ephemeral=True)
            return
        lines = []
        for key, menu in sorted(menus.items(), key=lambda kv: int(kv[0])):
            link = f"https://discord.com/channels/{ctx.guild.id}/{menu.get('channel')}/{menu.get('message')}"
            extra = " · one at a time" if menu.get("exclusive") else ""
            lines.append(f"**#{key}** [{menu.get('title')}]({link}) · {menu.get('style')} · "
                         f"{len(menu.get('items', []))} role(s){extra}")
        await ctx.reply(embed=embed("🎭 Role menus", "\n".join(lines) + "\n\n" + usage), ephemeral=True)

    @rolemenu.command(name="create", aliases=["new", "make"], description="Post a new, empty role menu")
    @app_commands.describe(title="The menu's title", style="Buttons, a dropdown, or reactions",
                           exclusive="Members can only hold one of its roles (good for colours)",
                           channel="Where to post it (default: here)", description="Text under the title")
    @moderator("manage_roles")
    async def rolemenu_create(self, ctx: commands.Context, title: str,
                              style: Literal["buttons", "dropdown", "reactions"] = "buttons",
                              exclusive: bool = False, channel: Optional[discord.TextChannel] = None, *,
                              description: str = "") -> None:
        channel = channel or ctx.channel
        data = self.data(ctx.guild.id)
        if len(data["menus"]) >= MAX_MENUS:
            await ctx.reply(embed=problem("Too many menus", f"{MAX_MENUS} is the limit. Delete one first."), ephemeral=True)
            return
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links) or (style == "reactions" and not perms.add_reactions):
            await ctx.reply(embed=problem("I can't post there", f"Give SYKE **Send Messages**, **Embed Links**"
                                                                f"{' and **Add Reactions**' if style == 'reactions' else ''}"
                                                                f" in {channel.mention}."), ephemeral=True)
            return
        if not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.reply(embed=problem("SYKE needs Manage Roles", "Give SYKE's role **Manage Roles** first."),
                            ephemeral=True)
            return
        number = int(data.get("next") or 1)
        menu = {"channel": channel.id, "title": title[:200], "text": description.replace("\\n", "\n")[:1500],
                "style": style, "exclusive": exclusive, "items": []}
        try:
            sent = await channel.send(embed=menu_embed(menu))
        except discord.HTTPException:
            await ctx.reply(embed=problem("Couldn't post it", f"Check SYKE can post in {channel.mention}."), ephemeral=True)
            return
        menu["message"] = sent.id
        data["menus"][str(number)] = menu
        data["next"] = number + 1
        self.save(ctx.guild.id, data)
        await ctx.reply(embed=embed(f"Menu #{number} posted 🎭",
                                    f"In {channel.mention}. Now add roles:\n`{self.p(ctx)}rolemenu add {number} @role 🎮 "
                                    f"[label]`", OK), ephemeral=True)

    @rolemenu.command(name="add", description="Add a role to a menu")
    @app_commands.describe(menu="The menu's number", role="The role to hand out", emoji="An emoji for it",
                           label="Button or option text (default: the role's name)")
    @moderator("manage_roles")
    async def rolemenu_add(self, ctx: commands.Context, menu: int, role: discord.Role, emoji: Optional[str] = None, *,
                           label: str = "") -> None:
        data = self.data(ctx.guild.id)
        found = data["menus"].get(str(menu))
        if found is None:
            await ctx.reply(f"There's no menu #{menu}. See `{self.p(ctx)}rolemenu`.", ephemeral=True)
            return
        issue = role_issue(role, ctx.guild, ctx.author)
        if issue:
            await ctx.reply(embed=problem("Can't use that role", issue), ephemeral=True)
            return
        stored, error = parse_emoji(emoji)
        if error and not label:  # "!rolemenu add 1 @role Gamers": the word was the label, not an emoji
            stored, error, label = None, None, emoji or ""
        if error:
            await ctx.reply(embed=problem("Hmm", error), ephemeral=True)
            return
        if found.get("style") == "reactions" and not stored:
            await ctx.reply(embed=problem("Needs an emoji", "Reaction menus need an emoji for every role."), ephemeral=True)
            return
        items = [i for i in found.get("items", []) if i["role"] != role.id]
        if len(items) >= MAX_ITEMS:
            await ctx.reply(embed=problem("Menu's full", f"A menu holds {MAX_ITEMS} roles."), ephemeral=True)
            return
        items.append({"role": role.id, "emoji": stored, "label": label.strip()[:80] or None})
        found["items"] = items
        self.save(ctx.guild.id, data)
        error = await self.refresh(ctx.guild, menu, found)
        await ctx.reply(embed=problem("Saved, but…", error) if error else
                        embed("Added", f"{role.mention} is on menu #{menu}.", OK), ephemeral=True)

    @rolemenu.command(name="remove", description="Take a role off a menu")
    @app_commands.describe(menu="The menu's number", role="The role to take off")
    @moderator("manage_roles")
    async def rolemenu_remove(self, ctx: commands.Context, menu: int, role: discord.Role) -> None:
        data = self.data(ctx.guild.id)
        found = data["menus"].get(str(menu))
        if found is None or role.id not in {i["role"] for i in found.get("items", [])}:
            await ctx.reply(f"{role.mention} isn't on menu #{menu}.", ephemeral=True)
            return
        found["items"] = [i for i in found["items"] if i["role"] != role.id]
        self.save(ctx.guild.id, data)
        error = await self.refresh(ctx.guild, menu, found)
        if found.get("style") == "reactions":
            with contextlib.suppress(Exception):
                channel = ctx.guild.get_channel(found["channel"])
                message = await channel.fetch_message(found["message"])
                await message.clear_reactions()
                await self.refresh(ctx.guild, menu, found)
        await ctx.reply(embed=problem("Saved, but…", error) if error else
                        embed("Removed", f"{role.mention} is off menu #{menu}.", OK), ephemeral=True)

    @rolemenu.command(name="edit", description="Change a menu's title or text")
    @app_commands.describe(menu="The menu's number", title="New title", description="New text (\\n for a new line)")
    @moderator("manage_roles")
    async def rolemenu_edit(self, ctx: commands.Context, menu: int, title: str, *, description: str = "") -> None:
        data = self.data(ctx.guild.id)
        found = data["menus"].get(str(menu))
        if found is None:
            await ctx.reply(f"There's no menu #{menu}.", ephemeral=True)
            return
        found["title"] = title[:200]
        found["text"] = description.replace("\\n", "\n")[:1500]
        self.save(ctx.guild.id, data)
        error = await self.refresh(ctx.guild, menu, found)
        await ctx.reply(embed=problem("Saved, but…", error) if error else embed("Updated", f"Menu #{menu} is updated.", OK),
                        ephemeral=True)

    @rolemenu.command(name="delete", description="Delete a menu and its message")
    @app_commands.describe(menu="The menu's number")
    @moderator("manage_roles")
    async def rolemenu_delete(self, ctx: commands.Context, menu: int) -> None:
        data = self.data(ctx.guild.id)
        found = data["menus"].pop(str(menu), None)
        if found is None:
            await ctx.reply(f"There's no menu #{menu}.", ephemeral=True)
            return
        self.save(ctx.guild.id, data)
        channel = ctx.guild.get_channel(found.get("channel") or 0)
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(found["message"]).delete()
        await ctx.reply(embed=embed("Deleted", f"Menu #{menu} is gone. The roles themselves are untouched.", WARN),
                        ephemeral=True)

    @rolemenu.command(name="colors", aliases=["colours", "color", "colour"],
                      description="Make colour roles and a pick-one colour menu in one go")
    @app_commands.describe(channel="Where to post it (default: here)")
    @moderator("manage_roles")
    async def rolemenu_colors(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or ctx.channel
        guild = ctx.guild
        if not guild.me.guild_permissions.manage_roles:
            await ctx.reply(embed=problem("SYKE needs Manage Roles", "Give SYKE's role **Manage Roles** first."),
                            ephemeral=True)
            return
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        roles = []
        made = 0
        for name, hex_value, icon in COLOURS:
            role = discord.utils.get(guild.roles, name=name)
            if role is None:
                try:
                    role = await guild.create_role(name=name, colour=discord.Colour.from_str(hex_value),
                                                   permissions=discord.Permissions.none(), reason="SYKE colour roles")
                    made += 1
                except discord.HTTPException:
                    await ctx.reply(embed=problem("Couldn't make the roles", "Discord refused. Check SYKE has "
                                                                             "**Manage Roles**."), ephemeral=True)
                    return
            if role_issue(role, guild):
                continue
            roles.append((role, icon))
        target = guild.me.top_role.position - 1
        with contextlib.suppress(discord.HTTPException):
            await guild.edit_role_positions({r: max(1, target) for r, _ in roles}, reason="Colour roles show above others")
        data = self.data(guild.id)
        number = int(data.get("next") or 1)
        menu = {"channel": channel.id, "title": "🎨 Pick your colour", "text": "Your name shows in the colour you pick.",
                "style": "dropdown", "exclusive": True, "colour": "#8B5CF6",
                "items": [{"role": r.id, "emoji": icon, "label": r.name} for r, icon in roles]}
        try:
            sent = await channel.send(embed=menu_embed(menu), view=menu_view(number, menu, guild))
        except discord.HTTPException:
            await ctx.reply(embed=problem("Couldn't post it", f"Check SYKE can post in {channel.mention}."), ephemeral=True)
            return
        menu["message"] = sent.id
        data["menus"][str(number)] = menu
        data["next"] = number + 1
        self.save(guild.id, data)
        await ctx.reply(embed=embed(f"Colour menu #{number} posted 🎨",
                                    f"In {channel.mention}, with {len(roles)} colours ({made} new roles). "
                                    f"Colours show when the colour roles sit above members' other coloured roles; "
                                    f"SYKE moved them right under its own role.", OK), ephemeral=True)
