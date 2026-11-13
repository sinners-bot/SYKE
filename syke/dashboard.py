"""!dashboard: every SYKE setting in one message, changed with menus and buttons."""

from __future__ import annotations

import contextlib
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from . import verify, welcome as welcome_mod
from .awards import DAYS, DEFAULT_DAY, DEFAULT_HOUR, parse_day, parse_hour, schedule_text
from .moderation import moderator

log = logging.getLogger("syke.dashboard")

BRAND = discord.Colour.from_str("#8B5CF6")
WARN = discord.Colour.from_str("#F59E0B")
TIMEOUT = 600
TEXT_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
SECTIONS = (
    ("overview", "📋", "Overview", "Everything at a glance"),
    ("general", "⚙️", "General", "Prefix, timezone, demo mode"),
    ("channels", "👁️", "Channels", "What SYKE reads and where it yaps"),
    ("verify", "🚪", "Verification", "Verified role, gate channel, panel"),
    ("welcome", "👋", "Welcome", "The message sent after someone verifies"),
    ("awards", "🏆", "Weekly awards", "Channel, schedule, winner role"),
    ("moderation", "🛡️", "Moderation", "Mod log and restrictions"),
)
AWARDS_KEY = "awards"


def _mention(guild, kind: str, object_id) -> str:
    if not object_id:
        return "not set"
    found = guild.get_role(object_id) if kind == "role" else guild.get_channel(object_id)
    return found.mention if found else f"missing {kind}"


def _channels(guild, ids) -> list:
    return [c for c in (guild.get_channel(i) for i in ids) if c is not None]


class Dashboard(discord.ui.View):
    def __init__(self, bot, guild: discord.Guild, owner_id: int, section: str = "overview") -> None:
        super().__init__(timeout=TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.owner_id = owner_id
        self.section = section
        self.status = ""
        self.message: discord.Message | None = None
        self.build()

    # Access

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This dashboard belongs to whoever opened it. Run `/dashboard` for your own.", ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    @property
    def prefix(self) -> str:
        return self.bot.display_prefix(self.guild.id)

    # Layout

    def build(self) -> None:
        self.clear_items()
        menu = discord.ui.Select(
            placeholder="Choose a section…", row=0,
            options=[discord.SelectOption(label=label, value=key, emoji=emoji, description=desc,
                                          default=key == self.section)
                     for key, emoji, label, desc in SECTIONS],
        )

        async def pick(interaction: discord.Interaction) -> None:
            self.section = menu.values[0]
            self.status = ""
            await self.refresh(interaction)

        menu.callback = pick
        self.add_item(menu)
        getattr(self, f"_controls_{self.section}")()

    def page(self) -> discord.Embed:
        embed = getattr(self, f"_page_{self.section}")()
        if self.status:
            embed.description = f"{self.status}\n\n{embed.description or ''}".strip()
        embed.set_footer(text=f"SYKE dashboard · only {self._owner_name()} can use these controls · closes after 10 min")
        return embed

    def _owner_name(self) -> str:
        member = self.guild.get_member(self.owner_id)
        return getattr(member, "display_name", None) or "the opener"

    async def refresh(self, interaction: discord.Interaction, status: str | None = None) -> None:
        if status is not None:
            self.status = status
        self.build()
        if interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(embed=self.page(), view=self)
            return
        await interaction.response.edit_message(embed=self.page(), view=self)

    def _button(self, label: str, callback, *, emoji: str | None = None, row: int = 4,
                style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    def _channel_select(self, placeholder: str, current: list, callback, *, row: int, many: bool = False) -> None:
        select = discord.ui.ChannelSelect(
            placeholder=placeholder, channel_types=TEXT_TYPES, row=row,
            min_values=0, max_values=25 if many else 1,
            default_values=current[:25],
        )

        async def chosen(interaction: discord.Interaction) -> None:
            await callback(interaction, select.values)

        select.callback = chosen
        self.add_item(select)

    def _role_select(self, placeholder: str, current, callback, *, row: int) -> None:
        select = discord.ui.RoleSelect(placeholder=placeholder, row=row, min_values=0, max_values=1,
                                       default_values=[current] if current is not None else [])

        async def chosen(interaction: discord.Interaction) -> None:
            await callback(interaction, select.values[0] if select.values else None)

        select.callback = chosen
        self.add_item(select)

    # Overview

    def _page_overview(self) -> discord.Embed:
        g, s = self.guild, self.bot.storage
        tracked = _channels(g, s.tracked(g.id))
        yaps = _channels(g, s.yap_channels(g.id))
        role_id, remove_id, gate_id, *_ = s.verification(g.id)
        welcome = s.get_config(g.id, welcome_mod.CONFIG_KEY)
        awards = s.get_config(g.id, AWARDS_KEY)
        modlog = s.modlog_channel(g.id)
        restricted = len({row[0] for row in s.restrictions(g.id) if row[2] is None or row[2] > time.time()})
        embed = discord.Embed(title=f"🎛️ SYKE dashboard — {g.name}", colour=BRAND,
                              description="Pick a section below to change it.")
        embed.add_field(name="⚙️ General", value=(
            f"Prefix `{self.prefix}` · Timezone {self.bot.tz_for(g.id)}\n"
            f"Demo mode {'on' if s.demo_mode(g.id) else 'off'}"), inline=False)
        embed.add_field(name="👁️ Channels", value=(
            f"Reading: {', '.join(c.mention for c in tracked) or 'nothing yet'}\n"
            f"Yapping in: {', '.join(c.mention for c in yaps) or 'nowhere'}")[:1024], inline=False)
        embed.add_field(name="🚪 Verification", value=(
            f"Role {_mention(g, 'role', role_id)} · takes {_mention(g, 'role', remove_id) if remove_id else 'nothing'}"
            f" · in {_mention(g, 'channel', gate_id) if gate_id else 'any channel'}"), inline=False)
        embed.add_field(name="👋 Welcome", value=(
            f"{_mention(g, 'channel', welcome.get('channel')) if welcome.get('channel') else 'off'}"), inline=True)
        embed.add_field(name="🏆 Weekly awards", value=(
            f"{_mention(g, 'channel', awards.get('channel')) if awards.get('channel') else 'off'}"
            + (f"\n{schedule_text(awards)}" if awards.get("channel") else "")), inline=True)
        embed.add_field(name="🛡️ Moderation", value=(
            f"Mod log {_mention(g, 'channel', modlog) if modlog else 'off'}\n{restricted} member(s) restricted"),
            inline=True)
        return embed

    def _controls_overview(self) -> None:
        async def reload(interaction: discord.Interaction) -> None:
            await self.refresh(interaction, "")
        self._button("Refresh", reload, emoji="🔄", row=1)

    # General

    def _page_general(self) -> discord.Embed:
        s = self.bot.storage
        return discord.Embed(title="⚙️ General", colour=BRAND, description=(
            f"**Prefix:** `{self.prefix}` (mentioning SYKE always works too)\n"
            f"**Timezone:** {self.bot.tz_for(self.guild.id)} (for late-night posting and the awards schedule)\n"
            f"**Demo mode:** {'on' if s.demo_mode(self.guild.id) else 'off'} (adds 5 fake members to rankings)"))

    def _controls_general(self) -> None:
        async def prefix(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(PrefixModal(self))

        async def tz(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(TimezoneModal(self))

        async def demo(interaction: discord.Interaction) -> None:
            on = not self.bot.storage.demo_mode(self.guild.id)
            self.bot.storage.set_demo_mode(self.guild.id, on)
            await self.refresh(interaction, f"✅ Demo mode {'on' if on else 'off'}.")

        self._button("Change prefix", prefix, emoji="⌨️", row=1)
        self._button("Change timezone", tz, emoji="🕐", row=1)
        self._button("Toggle demo mode", demo, emoji="🧪", row=1)

    # Channels

    def _page_channels(self) -> discord.Embed:
        g, s = self.guild, self.bot.storage
        tracked = _channels(g, s.tracked(g.id))
        yaps = _channels(g, s.yap_channels(g.id))
        return discord.Embed(title="👁️ Channels", colour=BRAND, description=(
            "**Watched channels** are what SYKE reads for profiles, awards, `guess` and yap.\n"
            "**Yap channels** are where it chimes in on its own.\n\n"
            f"**Watching:** {', '.join(c.mention for c in tracked) or 'nothing'}\n"
            f"**Yapping in:** {', '.join(c.mention for c in yaps) or 'nowhere'}\n\n"
            "Pick the full list in each menu. Clear a menu to remove everything from it.")[:4096])

    def _controls_channels(self) -> None:
        g, s = self.guild, self.bot.storage

        async def set_tracked(interaction: discord.Interaction, values) -> None:
            chosen = {v.id for v in values}
            current = set(s.tracked(g.id))
            for cid in current - chosen:
                s.untrack(g.id, cid)
            for cid in chosen - current:
                s.track(g.id, cid)
            self.bot.scanner.invalidate(g.id)
            await self.refresh(interaction, f"✅ Watching {len(chosen)} channel(s).")

        async def set_yap(interaction: discord.Interaction, values) -> None:
            chosen = {v.id for v in values}
            current = s.yap_channels(g.id)
            for cid in current - chosen:
                s.set_yap(g.id, cid, False)
            for cid in chosen - current:
                s.set_yap(g.id, cid, True)
            await self.refresh(interaction, f"✅ Yapping in {len(chosen)} channel(s).")

        self._channel_select("Channels SYKE reads…", _channels(g, s.tracked(g.id)), set_tracked, row=1, many=True)
        self._channel_select("Channels SYKE yaps in…", _channels(g, s.yap_channels(g.id)), set_yap, row=2, many=True)

    # Verification

    def _page_verify(self) -> discord.Embed:
        embed = verify.explain_current(self.bot, self.guild, self.prefix)
        embed.title = "🚪 Verification"
        return embed

    def _controls_verify(self) -> None:
        g, bot = self.guild, self.bot
        role_id, remove_id, gate_id, *_ = bot.storage.verification(g.id)

        async def grant_role(interaction: discord.Interaction, role) -> None:
            if role is None:
                await self.refresh(interaction, "Pick a role. To turn verification off, press **Turn off**.")
                return
            result = verify.set_grant_role(bot, g, role, prefix=self.prefix)
            await self.refresh(interaction, f"**{result.title}** {result.description}")

        async def take_role(interaction: discord.Interaction, role) -> None:
            result = verify.set_remove_role(bot, g, role, prefix=self.prefix)
            await self.refresh(interaction, f"**{result.title}** {result.description}")

        async def gate(interaction: discord.Interaction, values) -> None:
            result = (verify.set_channel(bot, g, values[0], prefix=self.prefix) if values
                      else verify.clear_channel(bot, g))
            await self.refresh(interaction, f"**{result.title}** {result.description}")

        async def panel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            result = await verify.post_panel(bot, g, interaction.channel, self.prefix)
            await self.refresh(interaction, f"**{result.title}** {result.description}")

        async def off(interaction: discord.Interaction) -> None:
            result = verify.turn_off(bot, g)
            await self.refresh(interaction, f"**{result.title}** {result.description}")

        self._role_select("Role members get when they verify…", g.get_role(role_id) if role_id else None,
                          grant_role, row=1)
        self._role_select("Role to take away when they verify…", g.get_role(remove_id) if remove_id else None,
                          take_role, row=2)
        self._channel_select("Only allow verifying in… (clear = anywhere)", _channels(g, [gate_id] if gate_id else []),
                             gate, row=3)
        self._button("Post the Verify button", panel, emoji="✅", row=4, style=discord.ButtonStyle.success)
        self._button("Turn off", off, emoji="⛔", row=4, style=discord.ButtonStyle.danger)

    # Welcome

    def _page_welcome(self) -> discord.Embed:
        config = self.bot.storage.get_config(self.guild.id, welcome_mod.CONFIG_KEY)
        embed = welcome_mod.describe(config, self.guild, self.prefix)
        embed.description = embed.description.split("\n\n`")[0]
        return embed

    def _controls_welcome(self) -> None:
        g, s = self.guild, self.bot.storage
        config = s.get_config(g.id, welcome_mod.CONFIG_KEY)

        async def channel(interaction: discord.Interaction, values) -> None:
            s.update_config(g.id, welcome_mod.CONFIG_KEY, channel=values[0].id if values else None)
            note = f"✅ Welcomes go to {values[0].mention}." if values else "✅ Welcome messages off."
            if values and not s.verification(g.id)[0]:
                note += " ⚠️ Set up verification too, or nobody gets welcomed."
            await self.refresh(interaction, note)

        async def design(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(WelcomeModal(self, s.get_config(g.id, welcome_mod.CONFIG_KEY)))

        async def ping(interaction: discord.Interaction) -> None:
            on = not s.get_config(g.id, welcome_mod.CONFIG_KEY).get("ping", True)
            s.update_config(g.id, welcome_mod.CONFIG_KEY, ping=on)
            await self.refresh(interaction, f"✅ Pinging new members: {'on' if on else 'off'}.")

        async def thumb(interaction: discord.Interaction) -> None:
            on = not s.get_config(g.id, welcome_mod.CONFIG_KEY).get("thumbnail", True)
            s.update_config(g.id, welcome_mod.CONFIG_KEY, thumbnail=on)
            await self.refresh(interaction, f"✅ Avatar thumbnail: {'on' if on else 'off'}.")

        async def test(interaction: discord.Interaction) -> None:
            community = self.bot.get_cog("Community")
            await interaction.response.defer()
            current = s.get_config(g.id, welcome_mod.CONFIG_KEY)
            sent = community is not None and await community.send_welcome(
                interaction.user, {**current, "ping": False}, channel=interaction.channel)
            await self.refresh(interaction, "✅ Test welcome sent in this channel." if sent
                               else "⚠️ Couldn't send the test here. Check SYKE can post embeds.")

        def link(field: str, label: str):
            async def chosen(interaction: discord.Interaction, values) -> None:
                s.update_config(g.id, welcome_mod.CONFIG_KEY, **{field: values[0].id if values else None})
                await self.refresh(interaction, f"✅ {label}: {values[0].mention}." if values else f"✅ {label} removed.")
            return chosen

        self._channel_select("Welcome channel… (clear = off)", _channels(g, [config.get("channel", 0)]), channel, row=1)
        self._channel_select("Roles channel to point them to… (optional)",
                             _channels(g, [config.get("roles_channel", 0)]), link("roles_channel", "Roles channel"), row=2)
        self._channel_select("Colour roles channel… (optional)",
                             _channels(g, [config.get("colors_channel", 0)]),
                             link("colors_channel", "Colour roles channel"), row=3)
        self._button("Edit design", design, emoji="✏️", row=4, style=discord.ButtonStyle.primary)
        self._button("Toggle ping", ping, emoji="🔔", row=4)
        self._button("Toggle avatar", thumb, emoji="🖼️", row=4)
        self._button("Send a test here", test, emoji="🧪", row=4, style=discord.ButtonStyle.success)

    # Weekly awards

    def _page_awards(self) -> discord.Embed:
        community = self.bot.get_cog("Community")
        if community is None:
            return discord.Embed(title="🏆 Weekly awards", description="Not available right now.", colour=WARN)
        embed = community.awards_settings(self.guild, self.prefix)
        embed.description = embed.description.rsplit("\n\n", 1)[0]
        return embed

    def _controls_awards(self) -> None:
        g, s = self.guild, self.bot.storage
        config = s.get_config(g.id, AWARDS_KEY)
        community = self.bot.get_cog("Community")

        async def channel(interaction: discord.Interaction, values) -> None:
            if values:
                s.update_config(g.id, AWARDS_KEY, channel=values[0].id, last=time.time())
                note = f"✅ Awards go to {values[0].mention}, {schedule_text(s.get_config(g.id, AWARDS_KEY))}."
            else:
                s.update_config(g.id, AWARDS_KEY, channel=None)
                note = "✅ Weekly awards off."
            await self.refresh(interaction, note)

        async def role(interaction: discord.Interaction, picked) -> None:
            if picked is None:
                s.update_config(g.id, AWARDS_KEY, role=None)
                await self.refresh(interaction, "✅ No winner role.")
                return
            from .community import role_problem
            issue = role_problem(g, picked)
            if issue:
                await self.refresh(interaction, f"⚠️ {issue}")
                return
            s.update_config(g.id, AWARDS_KEY, role=picked.id, holders=[])
            await self.refresh(interaction, f"✅ Winners get {picked.mention} for a week.")

        async def schedule(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AwardsModal(self, s.get_config(g.id, AWARDS_KEY)))

        async def preview(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            from .awards import awards_embed
            result = await community.build_awards(g)
            await interaction.followup.send(embed=awards_embed(result, g.name, preview=True), ephemeral=True)

        async def post(interaction: discord.Interaction) -> None:
            current = s.get_config(g.id, AWARDS_KEY)
            if not current.get("channel"):
                await self.refresh(interaction, "⚠️ Pick an awards channel first.")
                return
            await interaction.response.defer()
            posted, note = await community.post_awards(g, current)
            await self.refresh(interaction, (f"✅ Posted in <#{current['channel']}>. " if posted else "⚠️ ") + note)

        role_now = g.get_role(config["role"]) if config.get("role") else None
        self._channel_select("Awards channel… (clear = off)", _channels(g, [config.get("channel", 0)]), channel, row=1)
        self._role_select("Winner role… (clear = none)", role_now, role, row=2)
        self._button("Change schedule", schedule, emoji="🗓️", row=3)
        if community is not None:
            self._button("Preview (just you)", preview, emoji="👀", row=3)
            self._button("Post now", post, emoji="📣", row=3, style=discord.ButtonStyle.success)

    # Moderation

    def _page_moderation(self) -> discord.Embed:
        g, s = self.guild, self.bot.storage
        modlog = s.modlog_channel(g.id)
        now = time.time()
        restricted = len({row[0] for row in s.restrictions(g.id) if row[2] is None or row[2] > now})
        return discord.Embed(title="🛡️ Moderation", colour=BRAND, description=(
            f"**Mod log:** {_mention(g, 'channel', modlog) if modlog else 'off'}\n"
            f"**Restricted members:** {restricted} (`{self.prefix}restrictions` lists them)\n\n"
            "**Sync restrictions** re-applies the `SYKE · no …` roles to every channel and gives back any role "
            "a restricted member lost. Use it after moving channels around.\n\n"
            f"Commands: `{self.prefix}restrict`, `{self.prefix}timeout`, `{self.prefix}warn`, `{self.prefix}purge`, "
            f"`{self.prefix}lock`, `{self.prefix}kick`, `{self.prefix}ban` — see `{self.prefix}help`."))

    def _controls_moderation(self) -> None:
        g, s = self.guild, self.bot.storage

        async def modlog(interaction: discord.Interaction, values) -> None:
            s.set_modlog_channel(g.id, values[0].id if values else None)
            await self.refresh(interaction, f"✅ Mod log: {values[0].mention}." if values else "✅ Mod log off.")

        async def sync(interaction: discord.Interaction) -> None:
            moderation = self.bot.get_cog("Moderation")
            if moderation is None:
                await self.refresh(interaction, "⚠️ Moderation isn't loaded.")
                return
            await interaction.response.defer()
            changed = refused = 0
            for kind, role_id in s.restrict_roles(g.id).items():
                role = g.get_role(role_id)
                if role is not None:
                    done, failed = await moderation.apply_overwrites(g, role, kind)
                    changed, refused = changed + done, refused + failed
            fixed = await moderation.reapply_roles(g)
            note = f"✅ Updated {changed} overwrite(s), gave back {fixed} role(s)."
            if refused:
                note += f" ⚠️ {refused} channel(s) refused."
            await self.refresh(interaction, note)

        modlog_id = s.modlog_channel(g.id)
        self._channel_select("Mod log channel… (clear = off)", _channels(g, [modlog_id] if modlog_id else []),
                             modlog, row=1)
        self._button("Sync restrictions", sync, emoji="🔁", row=2)


# Modals

class PrefixModal(discord.ui.Modal, title="Change SYKE's prefix"):
    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__()
        self.dashboard = dashboard
        self.value = discord.ui.TextInput(label="New prefix (or 'mention' / 'reset')", max_length=10,
                                          default=dashboard.prefix.strip()[:10])
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from .bot import MAX_PREFIX_LENGTH, MENTION_ONLY, MENTION_WORDS
        bot, guild = self.dashboard.bot, self.dashboard.guild
        new = str(self.value.value).strip()
        if new.lower() in MENTION_WORDS:
            bot.set_prefix(guild.id, MENTION_ONLY)
            await self.dashboard.refresh(interaction, "✅ SYKE now only answers to @mentions.")
            return
        default = bot.settings.default_prefix
        if new.lower() == "reset":
            new = default
        if not new or len(new) > MAX_PREFIX_LENGTH or any(c.isspace() for c in new) or "`" in new or new.startswith("<"):
            await self.dashboard.refresh(interaction, f"⚠️ Use 1-{MAX_PREFIX_LENGTH} characters, no spaces or backticks.")
            return
        applied = bot.set_prefix(guild.id, None if new == default else new)
        await self.dashboard.refresh(interaction, f"✅ Prefix is now `{applied}`.")


class TimezoneModal(discord.ui.Modal, title="Change the timezone"):
    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__()
        self.dashboard = dashboard
        self.value = discord.ui.TextInput(label="Timezone, e.g. Europe/Vienna", max_length=64,
                                          default=str(dashboard.bot.tz_for(dashboard.guild.id)))
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from .bot import ALL_TIMEZONES
        wanted = str(self.value.value).strip().lower()
        match = next((tz for tz in ALL_TIMEZONES if tz.lower() == wanted), None)
        if match is None:
            await self.dashboard.refresh(interaction, f"⚠️ `{self.value.value}` isn't a timezone. Try `Europe/Berlin`.")
            return
        self.dashboard.bot.storage.set_timezone(self.dashboard.guild.id, match)
        await self.dashboard.refresh(interaction, f"✅ Timezone is now **{match}**.")


class WelcomeModal(discord.ui.Modal, title="Design the welcome message"):
    def __init__(self, dashboard: Dashboard, config: dict) -> None:
        super().__init__()
        self.dashboard = dashboard
        colour = config.get("colour")
        self.title_input = discord.ui.TextInput(label="Title", max_length=welcome_mod.TITLE_LIMIT, required=False,
                                                default=config.get("title") or welcome_mod.DEFAULT_TITLE)
        self.text_input = discord.ui.TextInput(
            label="Message ({roast} {mention} {next_steps} {count})", style=discord.TextStyle.paragraph,
            max_length=welcome_mod.TEXT_LIMIT, required=False, default=config.get("text") or welcome_mod.DEFAULT_TEXT,
        )
        self.colour_input = discord.ui.TextInput(label="Colour (hex, e.g. #ff8800)", max_length=7, required=False,
                                                 default=("#%06X" % colour) if isinstance(colour, int) else "")
        self.image_input = discord.ui.TextInput(label="Banner image or GIF link (optional)", max_length=500,
                                                required=False, default=config.get("image") or "")
        self.footer_input = discord.ui.TextInput(label="Footer (optional)", max_length=300, required=False,
                                                 default=config.get("footer") or "")
        for item in (self.title_input, self.text_input, self.colour_input, self.image_input, self.footer_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        storage, guild = self.dashboard.bot.storage, self.dashboard.guild
        colour_text = str(self.colour_input.value).strip()
        colour = welcome_mod.parse_colour(colour_text) if colour_text else None
        image = str(self.image_input.value).strip()
        problems = []
        if colour_text and colour is None:
            problems.append("the colour isn't a hex code")
        if image and not welcome_mod.URL.fullmatch(image):
            problems.append("the image isn't a link")
            image = ""
        title = str(self.title_input.value).strip()
        text = str(self.text_input.value).strip()
        storage.update_config(
            guild.id, welcome_mod.CONFIG_KEY,
            title=None if not title or title == welcome_mod.DEFAULT_TITLE else title,
            text=None if not text or text == welcome_mod.DEFAULT_TEXT else text,
            colour=colour, image=image or None, footer=str(self.footer_input.value).strip() or None,
        )
        note = "✅ Welcome design saved. Press **Send a test here** to see it."
        if problems:
            note += " ⚠️ Skipped: " + ", ".join(problems) + "."
        await self.dashboard.refresh(interaction, note)


class AwardsModal(discord.ui.Modal, title="Weekly awards schedule"):
    def __init__(self, dashboard: Dashboard, config: dict) -> None:
        super().__init__()
        self.dashboard = dashboard
        self.day = discord.ui.TextInput(label="Day of the week", max_length=12,
                                        default=DAYS[config.get("day", DEFAULT_DAY)].title())
        self.hour = discord.ui.TextInput(label="Hour (server timezone), e.g. 18 or 6pm", max_length=8,
                                         default=f"{config.get('hour', DEFAULT_HOUR):02d}:00")
        self.add_item(self.day)
        self.add_item(self.hour)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        day, hour = parse_day(str(self.day.value)), parse_hour(str(self.hour.value))
        if day is None or hour is None:
            await self.dashboard.refresh(interaction, "⚠️ Try a day like `sunday` and an hour like `18` or `6pm`.")
            return
        config = self.dashboard.bot.storage.update_config(self.dashboard.guild.id, AWARDS_KEY, day=day, hour=hour,
                                                          last=time.time())
        await self.dashboard.refresh(interaction, f"✅ Awards go out {schedule_text(config)}.")


class DashboardCog(commands.Cog, name="Dashboard"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="dashboard", aliases=["panel", "admin", "setup"],
                             description="Every SYKE setting in one place, with buttons")
    @moderator("manage_guild")
    async def dashboard(self, ctx: commands.Context) -> None:
        view = Dashboard(self.bot, ctx.guild, ctx.author.id)
        view.message = await ctx.reply(embed=view.page(), view=view, ephemeral=True)
