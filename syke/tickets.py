"""Support tickets: a panel button opens a private thread, staff claim it, closing saves a transcript.

Flow:
1. `ticket setup #channel @staff #logs` posts a panel with an "Open a ticket" button.
2. A member presses it, says what they need in a short form, and gets a private thread in that
   channel. Only they, the staff role and people with Manage Threads can see it.
3. Staff press "Claim" to take it. "Close" (or `ticket close [reason]`) saves an HTML transcript to
   the log channel, DMs a copy to the member, and locks and archives the thread.

The buttons use fixed custom ids and are registered as persistent views, so they keep working after
a restart. Open tickets live in the server's "tickets" config.
"""

from __future__ import annotations

import contextlib
import html
import io
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .moderation import BRAND, DANGER, OK, WARN, embed, moderator, problem

log = logging.getLogger("syke.tickets")

CONFIG = "tickets"
OPEN_ID = "syke:ticket:open"
CLAIM_ID = "syke:ticket:claim"
CLOSE_ID = "syke:ticket:close"
DEFAULT_TITLE = "Need help?"
DEFAULT_TEXT = ("Press **Open a ticket** and tell us what's up. You'll get a private thread with the staff team, "
                "nobody else can see it.")
DEFAULT_LIMIT = 1
TRANSCRIPT_LIMIT = 5000
TOPIC_MAX = 300


def staff_ids(config: dict) -> set[int]:
    role = config.get("staff")
    return {int(role)} if role else set()


def is_staff(member, config: dict) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild or perms.manage_threads):
        return True
    roles = {getattr(r, "id", None) for r in getattr(member, "roles", []) or []}
    return bool(staff_ids(config) & roles)


def thread_name(number: int, member) -> str:
    raw = (getattr(member, "name", "") or "member").lower()
    clean = re.sub(r"[^a-z0-9_-]+", "", raw)[:40] or "member"
    return f"ticket-{number:04d}-{clean}"


def open_for(config: dict, user_id: int) -> list[str]:
    return [tid for tid, t in (config.get("open") or {}).items() if t.get("user") == user_id]


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


# Transcripts

_MENTION = re.compile(r"<@!?(\d+)>|<@&(\d+)>|<#(\d+)>|<a?:(\w+):\d+>|<t:(\d+)(?::\w)?>")
_LINK = re.compile(r"(https?://[^\s<]+)")


def _render_content(text: str, names: dict[int, str]) -> str:
    def swap(m: re.Match) -> str:
        if m.group(1):
            return f"\x00@{names.get(int(m.group(1)), 'someone')}\x01"
        if m.group(2):
            return f"\x00@{names.get(int(m.group(2)), 'role')}\x01"
        if m.group(3):
            return f"\x00#{names.get(int(m.group(3)), 'channel')}\x01"
        if m.group(4):
            return f":{m.group(4)}:"
        stamp = datetime.fromtimestamp(int(m.group(5)), timezone.utc)
        return stamp.strftime("%d %b %Y %H:%M UTC")

    body = html.escape(_MENTION.sub(swap, text or ""))
    body = body.replace("\x00", '<span class="m">').replace("\x01", "</span>")
    body = _LINK.sub(r'<a href="\1">\1</a>', body)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", body)
    body = re.sub(r"(^|\n)(?:&gt;){1,3} ?", r"\1", body)
    return body.replace("\n", "<br>")


def message_record(message) -> dict:
    names: dict[int, str] = {}
    for user in getattr(message, "mentions", []) or []:
        names[user.id] = getattr(user, "display_name", None) or user.name
    for role in getattr(message, "role_mentions", []) or []:
        names[role.id] = role.name
    for channel in getattr(message, "channel_mentions", []) or []:
        names[channel.id] = channel.name
    author = message.author
    avatar = getattr(getattr(author, "display_avatar", None), "url", "") or ""
    return {
        "author": getattr(author, "display_name", None) or author.name,
        "username": author.name,
        "avatar": str(avatar),
        "bot": bool(getattr(author, "bot", False)),
        "at": message.created_at,
        "content": _render_content(message.content or "", names),
        "attachments": [(a.filename, a.url) for a in getattr(message, "attachments", []) or []],
        "embeds": [(e.title or "", e.description or "") for e in getattr(message, "embeds", []) or []
                   if e.title or e.description],
    }


TRANSCRIPT_CSS = """
body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 'gg sans','Segoe UI',Helvetica,Arial,sans-serif}
header{background:#2b2d31;padding:20px 28px;border-bottom:1px solid #1f2023}
header h1{margin:0 0 4px;font-size:20px;color:#f2f3f5}header p{margin:0;color:#b5bac1;font-size:13px}
main{padding:12px 0}.msg{display:flex;gap:14px;padding:6px 28px}.msg:hover{background:#2e3035}
.msg img.av{width:40px;height:40px;border-radius:50%;flex:none;background:#1e1f22}
.who{font-weight:600;color:#f2f3f5}.bot{background:#5865f2;color:#fff;font-size:10px;padding:1px 4px;
border-radius:3px;margin-left:5px;vertical-align:2px}.when{color:#949ba4;font-size:12px;margin-left:6px}
.text{word-wrap:break-word;white-space:normal}.m{background:#3c4270;color:#c9cdfb;border-radius:3px;padding:0 2px}
a{color:#00a8fc}.emb{border-left:4px solid #8b5cf6;background:#2b2d31;border-radius:4px;padding:8px 12px;
margin-top:6px;max-width:520px}.emb b{display:block;color:#f2f3f5}.file{margin-top:4px}
footer{color:#949ba4;font-size:12px;padding:16px 28px 28px}
code{background:#2b2d31;border-radius:3px;padding:1px 4px;font:13px Consolas,monospace}
img.pic{display:block;max-width:360px;max-height:260px;border-radius:6px;margin-top:6px}
"""


def transcript_html(title: str, subtitle: str, records: list[dict]) -> str:
    rows = []
    for r in records:
        when = r["at"].astimezone(timezone.utc).strftime("%d %b %Y %H:%M")
        bits = [f'<div class="text">{r["content"]}</div>'] if r["content"] else []
        for heading, desc in r["embeds"]:
            bits.append(f'<div class="emb"><b>{html.escape(heading)}</b>{_render_content(desc, {})}</div>')
        for name, url in r["attachments"]:
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                bits.append(f'<a href="{html.escape(url)}"><img class="pic" src="{html.escape(url)}" alt="{html.escape(name)}"></a>')
            bits.append(f'<div class="file">📎 <a href="{html.escape(url)}">{html.escape(name)}</a></div>')
        tag = '<span class="bot">BOT</span>' if r["bot"] else ""
        rows.append(
            f'<div class="msg"><img class="av" src="{html.escape(r["avatar"])}" alt="">'
            f'<div><span class="who" title="@{html.escape(r["username"])}">{html.escape(r["author"])}</span>{tag}'
            f'<span class="when">{when} UTC</span>{"".join(bits)}</div></div>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{TRANSCRIPT_CSS}</style></head><body>"
        f"<header><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></header>"
        f"<main>{''.join(rows) or '<p style=padding:0 28px>No messages.</p>'}</main>"
        f"<footer>{len(records)} message(s) · saved by SYKE</footer></body></html>"
    )


# Views

class TopicModal(discord.ui.Modal, title="Open a ticket"):
    topic = discord.ui.TextInput(label="What do you need help with?", style=discord.TextStyle.paragraph,
                                 max_length=TOPIC_MAX, required=True,
                                 placeholder="A few words so staff know what it's about")

    def __init__(self, cog: "Tickets") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, text = await self.cog.open_ticket(interaction.guild, interaction.user, str(self.topic.value))
        await interaction.followup.send(embed=embed("Ticket opened 🎫", text, OK) if ok else problem("No ticket", text),
                                        ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open a ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id=OPEN_ID)
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        config = self.cog.config(interaction.guild.id)
        if not config.get("channel"):
            await interaction.response.send_message("Tickets are switched off here.", ephemeral=True)
            return
        existing = open_for(config, interaction.user.id)
        if len(existing) >= int(config.get("limit") or DEFAULT_LIMIT):
            await interaction.response.send_message(f"You already have a ticket open: <#{existing[0]}>", ephemeral=True)
            return
        await interaction.response.send_modal(TopicModal(self.cog))


class ConfirmClose(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="Close it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Closing… saving the transcript.", view=None)
        await self.cog.close_ticket(interaction.channel, interaction.user, None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Still open.", view=None)


class ControlView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id=CLAIM_ID)
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        text = await self.cog.claim(interaction.channel, interaction.user)
        await interaction.response.send_message(text, ephemeral=not text.startswith("🙋"))

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=CLOSE_ID)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        reason = self.cog.can_close(interaction.channel, interaction.user)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.send_message("Close this ticket?", view=ConfirmClose(self.cog), ephemeral=True)


# The cog

class Tickets(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.closing: set[int] = set()

    async def cog_load(self) -> None:
        self.bot.add_view(PanelView(self))
        self.bot.add_view(ControlView(self))

    def config(self, guild_id: int) -> dict:
        return self.bot.storage.get_config(guild_id, CONFIG)

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    def ticket_of(self, channel) -> tuple[dict, dict | None]:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return {}, None
        config = self.config(guild.id)
        return config, (config.get("open") or {}).get(str(getattr(channel, "id", 0)))

    def panel_embed(self, config: dict) -> discord.Embed:
        card = embed(f"🎫 {config.get('title') or DEFAULT_TITLE}", config.get("text") or DEFAULT_TEXT)
        card.set_footer(text="Private · only you and staff can see your ticket")
        return card

    async def open_ticket(self, guild, member, topic: str) -> tuple[bool, str]:
        config = self.config(guild.id)
        channel = guild.get_channel(config.get("channel") or 0)
        if channel is None:
            return False, "Tickets aren't set up here. An admin can run `ticket setup`."
        existing = open_for(config, member.id)
        if len(existing) >= int(config.get("limit") or DEFAULT_LIMIT):
            return False, f"You already have a ticket open: <#{existing[0]}>"
        number = int(config.get("counter") or 0) + 1
        try:
            thread = await channel.create_thread(name=thread_name(number, member), type=discord.ChannelType.private_thread,
                                                 invitable=False, auto_archive_duration=10080,
                                                 reason=f"Ticket #{number} for {member}")
            await thread.add_user(member)
        except discord.HTTPException:
            log.warning("Couldn't open a ticket thread in %s", guild.id, exc_info=True)
            return False, ("SYKE couldn't make the thread. It needs **Create Private Threads**, **Manage Threads** "
                           f"and **Send Messages in Threads** in {channel.mention}.")
        tickets = dict(config.get("open") or {})
        tickets[str(thread.id)] = {"user": member.id, "n": number, "at": time.time(), "topic": topic[:TOPIC_MAX],
                                   "claimed": None}
        self.bot.storage.update_config(guild.id, CONFIG, counter=number, open=tickets)
        staff = guild.get_role(config.get("staff") or 0)
        card = embed(f"Ticket #{number:04d}", f"**{getattr(member, 'display_name', member.name)}** needs help with:\n"
                                               f">>> {topic[:TOPIC_MAX]}")
        card.set_footer(text="Staff: press Claim to take it · anyone here can press Close when it's sorted")
        ping = f"{member.mention}" + (f" {staff.mention}" if staff else "")
        with contextlib.suppress(discord.HTTPException):
            await thread.send(ping, embed=card, view=ControlView(self),
                              allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False))
        return True, f"Your ticket is open: {thread.mention}"

    def can_close(self, channel, user) -> str | None:
        config, ticket = self.ticket_of(channel)
        if ticket is None:
            return "This isn't an open ticket."
        if ticket.get("user") == user.id or is_staff(user, config):
            return None
        return "Only the person who opened it or staff can close it."

    async def claim(self, channel, user) -> str:
        config, ticket = self.ticket_of(channel)
        if ticket is None:
            return "This isn't an open ticket."
        if not is_staff(user, config):
            return "Only staff can claim tickets."
        if ticket.get("claimed"):
            return f"Already claimed by <@{ticket['claimed']}>."
        tickets = dict(config.get("open") or {})
        tickets[str(channel.id)] = {**ticket, "claimed": user.id}
        self.bot.storage.update_config(channel.guild.id, CONFIG, open=tickets)
        return f"🙋 {user.mention} has claimed this ticket and will help you out."

    async def transcript(self, thread, ticket: dict) -> tuple[discord.File, int]:
        records = []
        async for message in thread.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
            records.append(message_record(message))
        opened = datetime.fromtimestamp(ticket.get("at") or time.time(), timezone.utc).strftime("%d %b %Y %H:%M UTC")
        page = transcript_html(f"Ticket #{ticket.get('n', 0):04d} · {thread.name}",
                               f"{thread.guild.name} · opened {opened}", records)
        return discord.File(io.BytesIO(page.encode()), filename=f"transcript-{ticket.get('n', 0):04d}.html"), len(records)

    async def close_ticket(self, thread, closer, reason: str | None) -> str:
        config, ticket = self.ticket_of(thread)
        if ticket is None:
            return "This isn't an open ticket."
        if thread.id in self.closing:
            return "Already closing."
        self.closing.add(thread.id)
        try:
            guild = thread.guild
            log_card = embed(f"Ticket #{ticket.get('n', 0):04d} closed", "", BRAND)
            log_card.add_field(name="Opened by", value=f"<@{ticket.get('user')}>")
            log_card.add_field(name="Closed by", value=closer.mention)
            if ticket.get("claimed"):
                log_card.add_field(name="Claimed by", value=f"<@{ticket['claimed']}>")
            log_card.add_field(name="Open for", value=duration(time.time() - (ticket.get("at") or time.time())))
            log_card.add_field(name="Topic", value=(ticket.get("topic") or "—")[:1024], inline=False)
            if reason:
                log_card.add_field(name="Reason", value=reason[:1024], inline=False)
            try:
                file, count = await self.transcript(thread, ticket)
                data = file.fp.getvalue()
            except discord.HTTPException:
                data, count = b"", 0
            log_card.set_footer(text=f"{count} message(s) in the transcript")
            log_channel = guild.get_channel(config.get("log") or 0)
            if log_channel is not None and data:
                with contextlib.suppress(discord.HTTPException):
                    await log_channel.send(embed=log_card, file=discord.File(io.BytesIO(data), filename=file.filename))
            opener = guild.get_member(ticket.get("user") or 0)
            if opener is not None and data:
                dm = embed(f"Your ticket in {guild.name} was closed", reason or "Thanks for reaching out.", OK)
                with contextlib.suppress(discord.HTTPException, AttributeError):
                    await opener.send(embed=dm, file=discord.File(io.BytesIO(data), filename=file.filename))
            tickets = dict(config.get("open") or {})
            tickets.pop(str(thread.id), None)
            self.bot.storage.update_config(guild.id, CONFIG, open=tickets)
            closed = embed("🔒 Ticket closed", f"Closed by {closer.mention}" + (f": {reason}" if reason else "."), DANGER)
            with contextlib.suppress(discord.HTTPException):
                await thread.send(embed=closed)
            with contextlib.suppress(discord.HTTPException):
                await thread.edit(archived=True, locked=True, reason=f"Ticket closed by {closer}")
            return "Closed."
        finally:
            self.closing.discard(thread.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload) -> None:
        config = self.config(payload.guild_id)
        tickets = dict(config.get("open") or {})
        if tickets.pop(str(payload.thread_id), None) is not None:
            self.bot.storage.update_config(payload.guild_id, CONFIG, open=tickets)

    # Commands

    @commands.hybrid_group(name="ticket", aliases=["tickets"], fallback="open", case_insensitive=True,
                           description="Open a private support ticket")
    @app_commands.describe(topic="What you need help with")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context, *, topic: str = "") -> None:
        if not topic:
            await ctx.reply(embed=embed("🎫 Tickets", self.help_text(self.p(ctx))), ephemeral=True)
            return
        ok, text = await self.open_ticket(ctx.guild, ctx.author, topic)
        await ctx.reply(embed=embed("Ticket opened 🎫", text, OK) if ok else problem("No ticket", text), ephemeral=True)

    def help_text(self, p: str) -> str:
        return (f"`{p}ticket <what you need>` opens a private ticket, or press the button on the ticket panel.\n"
                f"In a ticket: `{p}ticket close [reason]`, `{p}ticket add @member`, `{p}ticket remove @member`.\n"
                f"Staff: `{p}ticket claim`, `{p}ticket rename <name>`.\n"
                f"Admins: `{p}ticket setup #channel @staff #logs`, `{p}ticket panel`, `{p}ticket settings`, "
                f"`{p}ticket off`.")

    @ticket.command(name="setup", description="Post the ticket panel and pick who handles tickets")
    @app_commands.describe(channel="Where the panel goes; tickets open as private threads here",
                           staff="The role that handles tickets", logs="Where closed-ticket transcripts go")
    @moderator("manage_guild")
    async def ticket_setup(self, ctx: commands.Context, channel: discord.TextChannel,
                           staff: Optional[discord.Role] = None, logs: Optional[discord.TextChannel] = None) -> None:
        perms = channel.permissions_for(ctx.guild.me)
        missing = [name for name, ok in (("Send Messages", perms.send_messages), ("Embed Links", perms.embed_links),
                                          ("Create Private Threads", perms.create_private_threads),
                                          ("Send Messages in Threads", perms.send_messages_in_threads),
                                          ("Manage Threads", perms.manage_threads)) if not ok]
        if missing:
            await ctx.reply(embed=problem("SYKE needs more permissions",
                                          f"Give SYKE {', '.join(f'**{m}**' for m in missing)} in {channel.mention}."),
                            ephemeral=True)
            return
        config = self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=channel.id,
                                                staff=staff.id if staff else None, log=logs.id if logs else None)
        try:
            panel = await channel.send(embed=self.panel_embed(config), view=PanelView(self))
        except discord.HTTPException:
            await ctx.reply(embed=problem("Couldn't post the panel", f"Check SYKE can post in {channel.mention}."),
                            ephemeral=True)
            return
        self.bot.storage.update_config(ctx.guild.id, CONFIG, panel=panel.id)
        body = f"Panel posted in {channel.mention}. Tickets open as private threads there."
        body += f"\nStaff role: {staff.mention}" if staff else "\nNo staff role: only people with Manage Threads see tickets."
        body += f"\nTranscripts go to {logs.mention}" if logs else "\nNo log channel: transcripts only go to the member."
        await ctx.reply(embed=embed("Tickets on 🎫", body, OK), ephemeral=True)

    @ticket.command(name="panel", description="Repost the ticket panel, optionally with your own title and text")
    @app_commands.describe(title="Panel title", text="Panel text (use \\n for a new line)")
    @moderator("manage_guild")
    async def ticket_panel(self, ctx: commands.Context, title: str = "", *, text: str = "") -> None:
        config = self.config(ctx.guild.id)
        channel = ctx.guild.get_channel(config.get("channel") or 0)
        if channel is None:
            await ctx.reply(embed=problem("Not set up", f"Run `{self.p(ctx)}ticket setup #channel` first."), ephemeral=True)
            return
        config = self.bot.storage.update_config(ctx.guild.id, CONFIG, title=title[:200] or None,
                                                text=text.replace("\\n", "\n")[:2000] or None)
        old = config.get("panel")
        if old:
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(old).delete()
        panel = await channel.send(embed=self.panel_embed(config), view=PanelView(self))
        self.bot.storage.update_config(ctx.guild.id, CONFIG, panel=panel.id)
        await ctx.reply(embed=embed("Panel reposted", f"In {channel.mention}.", OK), ephemeral=True)

    @ticket.command(name="settings", description="How tickets are set up here")
    @moderator("manage_guild")
    async def ticket_settings(self, ctx: commands.Context) -> None:
        config = self.config(ctx.guild.id)
        if not config.get("channel"):
            await ctx.reply(embed=embed("🎫 Tickets are off", f"Turn them on with `{self.p(ctx)}ticket setup #channel "
                                                             f"@staff #logs`."), ephemeral=True)
            return
        staff = f"<@&{config['staff']}>" if config.get("staff") else "Manage Threads only"
        logs = f"<#{config['log']}>" if config.get("log") else "DM only"
        lines = [f"**Panel:** <#{config['channel']}>", f"**Staff:** {staff}", f"**Transcripts:** {logs}",
                 f"**Open now:** {len(config.get('open') or {})}",
                 f"**Tickets so far:** {config.get('counter') or 0}"]
        await ctx.reply(embed=embed("🎫 Tickets", "\n".join(lines)), ephemeral=True)

    @ticket.command(name="off", description="Switch tickets off (open tickets stay until closed)")
    @moderator("manage_guild")
    async def ticket_off(self, ctx: commands.Context) -> None:
        config = self.config(ctx.guild.id)
        channel = ctx.guild.get_channel(config.get("channel") or 0)
        if channel is not None and config.get("panel"):
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(config["panel"]).delete()
        self.bot.storage.update_config(ctx.guild.id, CONFIG, channel=None, panel=None)
        await ctx.reply(embed=embed("Tickets off", "The panel is gone. Open tickets can still be closed.", WARN),
                        ephemeral=True)

    @ticket.command(name="close", description="Close this ticket and save a transcript")
    @app_commands.describe(reason="Why it's being closed")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket_close(self, ctx: commands.Context, *, reason: str = "") -> None:
        problem_text = self.can_close(ctx.channel, ctx.author)
        if problem_text:
            await ctx.reply(problem_text, ephemeral=True)
            return
        if ctx.interaction:
            await ctx.reply("Closing… saving the transcript.", ephemeral=True)
        await self.close_ticket(ctx.channel, ctx.author, reason[:1000] or None)

    @ticket.command(name="claim", description="Take this ticket (staff)")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket_claim(self, ctx: commands.Context) -> None:
        await ctx.reply(await self.claim(ctx.channel, ctx.author))

    async def _staff_in_ticket(self, ctx) -> tuple[dict, dict | None]:
        config, ticket = self.ticket_of(ctx.channel)
        if ticket is None:
            await ctx.reply("Use this inside an open ticket.", ephemeral=True)
            return config, None
        if not (is_staff(ctx.author, config) or ticket.get("user") == ctx.author.id):
            await ctx.reply("Only the ticket's owner or staff can do that.", ephemeral=True)
            return config, None
        return config, ticket

    @ticket.command(name="add", description="Add someone to this ticket")
    @app_commands.describe(member="Who to add")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket_add(self, ctx: commands.Context, member: discord.Member) -> None:
        _, ticket = await self._staff_in_ticket(ctx)
        if ticket is None:
            return
        try:
            await ctx.channel.add_user(member)
        except discord.HTTPException:
            await ctx.reply("Couldn't add them. SYKE needs Manage Threads.", ephemeral=True)
            return
        await ctx.reply(f"Added {member.mention} to the ticket.")

    @ticket.command(name="remove", description="Remove someone from this ticket")
    @app_commands.describe(member="Who to remove")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        _, ticket = await self._staff_in_ticket(ctx)
        if ticket is None:
            return
        if member.id == ticket.get("user"):
            await ctx.reply("That's the person the ticket is for. Close it instead.", ephemeral=True)
            return
        try:
            await ctx.channel.remove_user(member)
        except discord.HTTPException:
            await ctx.reply("Couldn't remove them. SYKE needs Manage Threads.", ephemeral=True)
            return
        await ctx.reply(f"Removed {member.mention} from the ticket.")

    @ticket.command(name="rename", description="Rename this ticket (staff)")
    @app_commands.describe(name="The new name")
    @app_commands.guild_only()
    @commands.guild_only()
    async def ticket_rename(self, ctx: commands.Context, *, name: str) -> None:
        config, ticket = self.ticket_of(ctx.channel)
        if ticket is None or not is_staff(ctx.author, config):
            await ctx.reply("Staff can rename open tickets, from inside the ticket.", ephemeral=True)
            return
        clean = f"{ticket.get('n', 0):04d}-{name}"[:100]
        with contextlib.suppress(discord.HTTPException):
            await ctx.channel.edit(name=clean)
        await ctx.reply(f"Renamed to **{clean}**.")
