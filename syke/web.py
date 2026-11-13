"""The owner web dashboard: every server SYKE is in, their channels and chats, and controls.

Locked to SYKE_OWNER_IDS through Discord OAuth. Runs in the bot's own event loop, so handlers can
touch discord.py objects directly. Turned on only when a client id, secret and session secret are set
(see Settings.web_enabled); otherwise the bot runs exactly as before.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import quote
from typing import Awaitable, Callable, Optional

import discord
import httpx
from aiohttp import web

from . import web_auth as auth
from .moderation import KINDS, human_duration, parse_duration

log = logging.getLogger("syke.web")

MESSAGES_SHOWN = 60
STORED_PREVIEW = 40
BRAND = "#8B5CF6"


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def base_url(request: web.Request, settings) -> str:
    if settings.web_base_url:
        return settings.web_base_url
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


# HTML

STYLE = """
:root{--bg:#0f0f14;--panel:#1a1a22;--panel2:#22222c;--line:#2d2d3a;--text:#e6e6ef;
--muted:#9a9ab0;--brand:#8B5CF6;--danger:#EF4444;--ok:#22C55E;--warn:#F59E0B;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;}
a{color:var(--brand);text-decoration:none;}a:hover{text-decoration:underline;}
header{position:sticky;top:0;background:#14141b;border-bottom:1px solid var(--line);
padding:12px 20px;display:flex;align-items:center;gap:14px;z-index:5;}
header .brand{font-weight:700;color:var(--brand);font-size:17px;}
header .sp{flex:1;}
.wrap{max-width:1100px;margin:0 auto;padding:20px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;}
.card h3{margin:0 0 6px;font-size:16px;}
.muted{color:var(--muted);font-size:13px;}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
padding:2px 10px;font-size:12px;color:var(--muted);}
.btn{display:inline-block;background:var(--panel2);border:1px solid var(--line);color:var(--text);
border-radius:8px;padding:7px 12px;font-size:14px;cursor:pointer;}
.btn:hover{border-color:var(--brand);text-decoration:none;}
.btn.brand{background:var(--brand);border-color:var(--brand);color:#fff;}
.btn.danger{background:var(--danger);border-color:var(--danger);color:#fff;}
.btn.sm{padding:4px 9px;font-size:13px;}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--text);
border-radius:8px;padding:8px;font:inherit;width:100%;}
label{display:block;font-size:13px;color:var(--muted);margin:8px 0 3px;}
table{width:100%;border-collapse:collapse;}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;}
.msg{padding:8px 0;border-bottom:1px solid var(--line);}
.msg .who{font-weight:600;}.msg .when{color:var(--muted);font-size:12px;margin-left:6px;}
.msg .body{white-space:pre-wrap;word-break:break-word;margin-top:2px;}
.note{background:var(--panel2);border-left:3px solid var(--brand);padding:8px 12px;border-radius:6px;margin:10px 0;}
.note.err{border-color:var(--danger);}.note.ok{border-color:var(--ok);}
.cols{display:grid;grid-template-columns:260px 1fr;gap:18px;}
.list a{display:block;padding:6px 8px;border-radius:7px;color:var(--text);}
.list a:hover{background:var(--panel2);text-decoration:none;}
.list a.on{background:var(--panel2);color:var(--brand);}
form.inline{display:inline;}
h1{font-size:22px;margin:0 0 4px;}h2{font-size:17px;margin:22px 0 10px;}
@media(max-width:760px){.cols{grid-template-columns:1fr;}}
"""


def layout(title: str, body: str, *, user: Optional[tuple[int, str]] = None) -> web.Response:
    nav = ""
    if user is not None:
        nav = (f'<span class="muted">{esc(user[1])}</span>'
               f'<a class="btn sm" href="/logout">Log out</a>')
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · SYKE</title><style>{STYLE}</style></head><body>"
        f"<header><span class='brand'>SYKE</span> <a class='btn sm' href='/'>Servers</a>"
        f"<span class='sp'></span>{nav}</header><div class='wrap'>{body}</div></body></html>"
    )
    return web.Response(text=page, content_type="text/html")


def flash(request: web.Request) -> str:
    kind = request.query.get("ok")
    if kind:
        return f"<div class='note ok'>{esc(kind)}</div>"
    err = request.query.get("err")
    if err:
        return f"<div class='note err'>{esc(err)}</div>"
    return ""


class WebDashboard:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self._add_routes()

    # Lifecycle

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.settings.web_port)
        await site.start()
        log.info("Owner dashboard listening on port %s", self.settings.web_port)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    def _add_routes(self) -> None:
        a = self.app.router.add_get
        p = self.app.router.add_post
        a("/", self.home)
        a("/login", self.login)
        a("/callback", self.callback)
        a("/logout", self.logout)
        a("/g/{gid}", self.server)
        a("/g/{gid}/c/{cid}", self.channel)
        p("/g/{gid}/c/{cid}/say", self.say)
        p("/g/{gid}/mod", self.moderate)
        p("/g/{gid}/settings", self.change_setting)
        p("/g/{gid}/leave", self.leave)
        p("/g/{gid}/rescan", self.rescan)
        p("/g/{gid}/clear", self.clear_data)

    # Auth plumbing

    def _user(self, request: web.Request) -> tuple[int, str] | None:
        token = request.cookies.get(auth.SESSION_COOKIE, "")
        return auth.session_user(self.settings.web_secret, token, self.settings.owner_ids)

    def _secure(self, request: web.Request) -> bool:
        return base_url(request, self.settings).startswith("https")

    def _set_session(self, response: web.Response, token: str, request: web.Request) -> None:
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                            samesite="Lax", secure=self._secure(request), path="/")

    def _check_csrf(self, request: web.Request, data, user_id: int) -> bool:
        return auth.valid_csrf(self.settings.web_secret, data.get("csrf", ""), user_id)

    # OAuth

    async def login(self, request: web.Request) -> web.Response:
        if self._user(request) is not None:
            raise web.HTTPFound("/")
        state = auth.make_state(self.settings.web_secret)
        url = auth.authorize_url(self.settings.web_client_id, auth.redirect_uri(base_url(request, self.settings)),
                                 state)
        raise web.HTTPFound(url)

    async def callback(self, request: web.Request) -> web.Response:
        if not auth.valid_state(self.settings.web_secret, request.query.get("state", "")):
            return layout("Login", "<div class='note err'>That login link expired. "
                                   "<a href='/login'>Try again.</a></div>")
        code = request.query.get("code", "")
        if not code:
            raise web.HTTPFound("/login")
        redirect = auth.redirect_uri(base_url(request, self.settings))
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                token_resp = await client.post(
                    auth.OAUTH_TOKEN,
                    data=auth.token_request(self.settings.web_client_id, self.settings.web_client_secret, code,
                                            redirect),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                token_resp.raise_for_status()
                access = token_resp.json().get("access_token", "")
                me = await client.get(auth.OAUTH_USER, headers={"Authorization": f"Bearer {access}"})
                me.raise_for_status()
                who = me.json()
        except httpx.HTTPError:
            log.warning("OAuth exchange failed", exc_info=True)
            return layout("Login", "<div class='note err'>Discord login failed. "
                                   "<a href='/login'>Try again.</a></div>")
        uid = int(who.get("id", 0))
        if uid not in self.settings.owner_ids:
            log.warning("Non-owner %s (%s) tried to sign in", who.get("username"), uid)
            return layout("Not allowed", "<div class='note err'>This dashboard is for SYKE's owners only.</div>")
        name = who.get("global_name") or who.get("username") or "owner"
        response = web.HTTPFound("/")
        self._set_session(response, auth.make_session(self.settings.web_secret, uid, name), request)
        raise response

    async def logout(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/login")
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        raise response

    # Helpers

    def _csrf(self, user_id: int) -> str:
        return auth.csrf_token(self.settings.web_secret, user_id)

    def _hidden_csrf(self, user_id: int) -> str:
        return f"<input type='hidden' name='csrf' value='{esc(self._csrf(user_id))}'>"

    def _guild_or_404(self, request: web.Request) -> discord.Guild:
        try:
            gid = int(request.match_info["gid"])
        except (KeyError, ValueError):
            raise web.HTTPNotFound(text="bad server id")
        guild = self.bot.get_guild(gid)
        if guild is None:
            raise web.HTTPNotFound(text="SYKE isn't in that server")
        return guild

    async def _post_guild(self, request: web.Request) -> tuple[discord.Guild, dict, tuple[int, str]]:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        data = dict(await request.post())
        if not self._check_csrf(request, data, user[0]):
            raise web.HTTPForbidden(text="bad or missing CSRF token")
        return guild, data, user

    @staticmethod
    def _redirect(to: str, *, ok: str = "", err: str = "") -> web.Response:
        sep = "&" if "?" in to else "?"
        if ok:
            to = f"{to}{sep}ok={quote(ok)}"
        elif err:
            to = f"{to}{sep}err={quote(err)}"
        return web.HTTPFound(to)

    # Pages

    async def home(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            return layout("Sign in", (
                "<h1>SYKE owner dashboard</h1>"
                "<p class='muted'>Every server SYKE is in, their channels and chats, "
                "and controls. Owners only.</p>"
                "<p><a class='btn brand' href='/login'>Log in with Discord</a></p>"
            ))
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        servers, members = self.bot.reach()
        cards = []
        for g in guilds:
            tracked = len(self.bot.storage.tracked(g.id))
            stored, _ = self.bot.storage.corpus_size(g.id)
            icon = f"<img src='{esc(g.icon.url)}' width='40' height='40' style='border-radius:8px'>" if g.icon else ""
            cards.append(
                f"<div class='card'><div class='row'>{icon}<div>"
                f"<h3><a href='/g/{g.id}'>{esc(g.name)}</a></h3>"
                f"<div class='muted'>{g.member_count or 0} members</div></div></div>"
                f"<div class='row' style='margin-top:10px'>"
                f"<span class='chip'>{tracked} watched</span>"
                f"<span class='chip'>{stored} stored msgs</span>"
                f"<span class='chip'>prefix {esc(self.bot.display_prefix(g.id))}</span></div></div>"
            )
        body = (f"{flash(request)}<h1>{servers} servers · {members:,} members</h1>"
                f"<div class='grid' style='margin-top:14px'>{''.join(cards)}</div>")
        return layout("Servers", body, user=user)

    async def server(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        csrf = self._hidden_csrf(user[0])
        tracked = set(self.bot.storage.tracked(guild.id))
        rows = []
        for ch in guild.text_channels:
            can = ch.permissions_for(guild.me).read_message_history
            star = "⭐ " if ch.id in tracked else ""
            link = f"<a href='/g/{guild.id}/c/{ch.id}'>#{esc(ch.name)}</a>" if can else f"#{esc(ch.name)}"
            track_btn = (f"<form class='inline' method='post' action='/g/{guild.id}/settings'>{csrf}"
                         f"<input type='hidden' name='field' value='{'untrack' if ch.id in tracked else 'track'}'>"
                         f"<input type='hidden' name='value' value='{ch.id}'>"
                         f"<button class='btn sm'>{'Untrack' if ch.id in tracked else 'Track'}</button></form>")
            rows.append(f"<tr><td>{star}{link}</td><td class='muted'>{'' if can else 'no access'}</td>"
                        f"<td>{track_btn}</td></tr>")
        prefix = esc(self.bot.display_prefix(guild.id))
        tz = esc(self.bot.storage.timezone(guild.id) or self.settings.default_timezone)
        settings_form = (
            f"<form method='post' action='/g/{guild.id}/settings' class='card'>{csrf}"
            f"<h3>Settings</h3>"
            f"<label>Prefix</label><input name='prefix' value='{prefix}' maxlength='5'>"
            f"<label>Timezone</label><input name='timezone' value='{tz}'>"
            f"<input type='hidden' name='field' value='bulk'>"
            f"<div style='margin-top:10px'><button class='btn brand'>Save</button></div></form>"
        )
        danger = (
            f"<div class='card'><h3>Maintenance</h3>"
            f"<div class='row'>"
            f"<form method='post' action='/g/{guild.id}/rescan'>{csrf}<button class='btn sm'>Rescan now</button></form>"
            f"<form method='post' action='/g/{guild.id}/clear' onsubmit=\"return confirm('Delete all stored "
            f"messages for this server?')\">{csrf}<button class='btn sm'>Clear stored data</button></form>"
            f"<form method='post' action='/g/{guild.id}/leave' onsubmit=\"return confirm('Make SYKE leave "
            f"{esc(guild.name)}?')\">{csrf}<button class='btn danger sm'>Leave server</button></form>"
            f"</div></div>"
        )
        mod = self._mod_form(guild, user[0])
        body = (
            f"{flash(request)}<h1>{esc(guild.name)}</h1>"
            f"<div class='muted'>{guild.member_count or 0} members · owner "
            f"{esc(getattr(guild.owner, 'display_name', guild.owner_id))}</div>"
            f"<div class='grid' style='margin-top:14px'>{settings_form}{mod}{danger}</div>"
            f"<h2>Channels</h2><table>{''.join(rows) or '<tr><td>No text channels</td></tr>'}</table>"
        )
        return layout(guild.name, body, user=user)

    def _mod_form(self, guild: discord.Guild, user_id: int) -> str:
        csrf = self._hidden_csrf(user_id)
        kinds = "".join(f"<label class='muted'><input type='checkbox' style='width:auto' name='kind' "
                        f"value='{k}'> {esc(v[0])}</label>" for k, v in KINDS.items())
        return (
            f"<form method='post' action='/g/{guild.id}/mod' class='card'>{csrf}"
            f"<h3>Moderate</h3>"
            f"<label>Member ID</label><input name='user_id' placeholder='e.g. 1342786189576634398' required>"
            f"<label>Action</label><select name='action'>"
            f"<option value='timeout'>Timeout</option><option value='restrict'>Restrict</option>"
            f"<option value='kick'>Kick</option><option value='ban'>Ban</option>"
            f"<option value='unrestrict'>Unrestrict</option><option value='untimeout'>Untimeout</option>"
            f"<option value='unban'>Unban</option></select>"
            f"<label>Duration (timeout/restrict, e.g. 10m, 2h, blank = forever)</label><input name='duration'>"
            f"<div class='row' style='margin-top:6px'>{kinds}</div>"
            f"<label>Reason</label><input name='reason'>"
            f"<div style='margin-top:10px'><button class='btn brand'>Apply</button></div></form>"
        )

    async def channel(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        try:
            cid = int(request.match_info["cid"])
        except ValueError:
            raise web.HTTPNotFound(text="bad channel id")
        channel = guild.get_channel(cid)
        if channel is None or not hasattr(channel, "history") or not hasattr(channel, "send"):
            raise web.HTTPNotFound(text="no such channel")
        me = guild.me
        can_read = channel.permissions_for(me).read_message_history
        can_send = channel.permissions_for(me).send_messages
        items = []
        if can_read:
            try:
                async for m in channel.history(limit=MESSAGES_SHOWN):
                    items.append(self._render_message(m.author.display_name, m.created_at.timestamp(),
                                                      m.clean_content, bot=m.author.bot))
            except discord.HTTPException:
                items.append("<div class='note err'>Couldn't read live history.</div>")
            items.reverse()
        else:
            items.append("<div class='note'>SYKE can't read live history here. Showing stored messages.</div>")
            for author_id, content in self.bot.storage.corpus(guild.id)[:STORED_PREVIEW]:
                items.append(self._render_message(f"user {author_id}", None, content))
        csrf = self._hidden_csrf(user[0])
        say = ""
        if can_send:
            say = (f"<form method='post' action='/g/{guild.id}/c/{channel.id}/say' class='card'>{csrf}"
                   f"<h3>Send as SYKE</h3><textarea name='content' rows='3' maxlength='2000' "
                   f"placeholder='Message to post in #{esc(channel.name)}' required></textarea>"
                   f"<div style='margin-top:8px'><button class='btn brand'>Send</button></div></form>")
        else:
            say = "<div class='note'>SYKE can't send messages in this channel.</div>"
        body = (
            f"{flash(request)}<div class='muted'><a href='/g/{guild.id}'>← {esc(guild.name)}</a></div>"
            f"<h1>#{esc(channel.name)}</h1>{say}"
            f"<div class='card' style='margin-top:14px'>{''.join(items) or 'No messages.'}</div>"
        )
        return layout(f"#{channel.name}", body, user=user)

    def _render_message(self, who: str, ts: float | None, content: str, *, bot: bool = False) -> str:
        when = f"<span class='when'>&lt;t:{int(ts)}&gt;</span>" if ts else ""
        tag = " 🤖" if bot else ""
        return (f"<div class='msg'><span class='who'>{esc(who)}{tag}</span>{when}"
                f"<div class='body'>{esc(content) or '<span class=\"muted\">(no text)</span>'}</div></div>")

    # Actions

    async def say(self, request: web.Request) -> web.Response:
        guild, data, _ = await self._post_guild(request)
        cid = int(request.match_info["cid"])
        channel = guild.get_channel(cid)
        content = (data.get("content") or "").strip()
        back = f"/g/{guild.id}/c/{cid}"
        if channel is None or not hasattr(channel, "send") or not content:
            raise self._redirect(back, err="Nothing to send.")
        try:
            await channel.send(content[:2000], allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        except discord.HTTPException:
            raise self._redirect(back, err="Discord refused to send that.")
        log.info("Owner sent a message as SYKE in %s/#%s", guild.id, getattr(channel, "name", cid))
        raise self._redirect(back, ok="Sent.")

    async def moderate(self, request: web.Request) -> web.Response:
        guild, data, user = await self._post_guild(request)
        back = f"/g/{guild.id}"
        try:
            target_id = int((data.get("user_id") or "").strip())
        except ValueError:
            raise self._redirect(back, err="That member ID isn't a number.")
        action = (data.get("action") or "").strip()
        reason = (data.get("reason") or "").strip()[:400] or "via owner dashboard"
        duration = (data.get("duration") or "").strip()
        seconds = parse_duration(duration) if duration else None
        kinds = [k for k in (await request.post()).getall("kind", []) if k in KINDS]
        try:
            message = await self._do_moderation(guild, target_id, action, reason, seconds, kinds, user)
        except discord.Forbidden:
            raise self._redirect(back, err="Discord refused. Check SYKE's role and permissions.")
        except discord.HTTPException as exc:
            raise self._redirect(back, err=f"Discord error: {exc.text[:80] if hasattr(exc, 'text') else exc}")
        except ValueError as exc:
            raise self._redirect(back, err=str(exc))
        raise self._redirect(back, ok=message)

    async def _do_moderation(self, guild, target_id, action, reason, seconds, kinds, user) -> str:
        storage = self.bot.storage
        mod = self.bot.get_cog("Moderation")
        member = guild.get_member(target_id)
        if member is None and action not in ("ban", "unban"):
            try:
                member = await guild.fetch_member(target_id)
            except discord.NotFound:
                raise ValueError("That member isn't in the server.")

        if action == "timeout":
            if not seconds:
                raise ValueError("Give a duration like 10m for a timeout.")
            await member.timeout(timedelta(seconds=min(seconds, 28 * 86400)), reason=reason)
            storage.add_case(guild.id, target_id, user[0], "timeout", reason, seconds)
            return f"Timed out {member.display_name} for {human_duration(seconds)}."
        if action == "untimeout":
            await member.timeout(None, reason=reason)
            storage.add_case(guild.id, target_id, user[0], "untimeout", reason)
            return f"Timeout ended for {member.display_name}."
        if action == "kick":
            await member.kick(reason=reason)
            storage.add_case(guild.id, target_id, user[0], "kick", reason)
            return f"Kicked {member.display_name}."
        if action == "ban":
            await guild.ban(discord.Object(id=target_id), reason=reason, delete_message_seconds=0)
            storage.add_case(guild.id, target_id, user[0], "ban", reason)
            return f"Banned {target_id}."
        if action == "unban":
            await guild.unban(discord.Object(id=target_id), reason=reason)
            storage.add_case(guild.id, target_id, user[0], "unban", reason)
            return f"Unbanned {target_id}."
        if action in ("restrict", "unrestrict"):
            if mod is None:
                raise ValueError("Moderation isn't loaded.")
            chosen = tuple(kinds) if kinds else ("images", "links")
            if action == "restrict":
                roles = []
                for kind in chosen:
                    role, created = await mod.ensure_role(guild, kind)
                    roles.append(role)
                    if created:
                        mod.queue_overwrites(guild, role, kind)
                await member.add_roles(*roles, reason=reason)
                expires = time.time() + seconds if seconds else None
                for kind in chosen:
                    storage.restrict(guild.id, target_id, kind, expires_at=expires, reason=reason,
                                     moderator_id=user[0])
                mod.forget_cache(guild.id)
                storage.add_case(guild.id, target_id, user[0], f"restrict ({', '.join(chosen)})", reason, seconds)
                return f"Restricted {member.display_name} from {', '.join(chosen)}."
            lifted = storage.unrestrict(guild.id, target_id, list(kinds) if kinds else None)
            roles = [r for r in await mod.roles_for(guild, lifted or KINDS) if r in getattr(member, 'roles', [])]
            if roles:
                await member.remove_roles(*roles, reason=reason)
            mod.forget_cache(guild.id)
            storage.add_case(guild.id, target_id, user[0], f"unrestrict ({', '.join(lifted) or 'all'})", reason)
            return f"Lifted restrictions on {member.display_name}."
        raise ValueError("Unknown action.")

    async def change_setting(self, request: web.Request) -> web.Response:
        guild, data, _ = await self._post_guild(request)
        back = f"/g/{guild.id}"
        field = (data.get("field") or "").strip()
        storage = self.bot.storage
        if field == "track":
            storage.track(guild.id, int(data["value"]))
            self.bot.scanner.invalidate(guild.id)
            raise self._redirect(back, ok="Channel tracked.")
        if field == "untrack":
            storage.untrack(guild.id, int(data["value"]))
            self.bot.scanner.invalidate(guild.id)
            raise self._redirect(back, ok="Channel untracked.")
        if field == "bulk":
            prefix = (data.get("prefix") or "").strip()
            if prefix and len(prefix) <= 5 and not any(c.isspace() for c in prefix):
                self.bot.set_prefix(guild.id, None if prefix == self.settings.default_prefix else prefix)
            tz = (data.get("timezone") or "").strip()
            from zoneinfo import available_timezones
            if tz and tz in available_timezones():
                storage.set_timezone(guild.id, tz)
            raise self._redirect(back, ok="Settings saved.")
        raise self._redirect(back, err="Unknown setting.")

    async def leave(self, request: web.Request) -> web.Response:
        guild, _, _ = await self._post_guild(request)
        name = guild.name
        try:
            await guild.leave()
        except discord.HTTPException:
            raise self._redirect(f"/g/{guild.id}", err="Couldn't leave right now.")
        log.info("Owner made SYKE leave %s (%s)", name, guild.id)
        raise self._redirect("/", ok=f"Left {name}.")

    async def rescan(self, request: web.Request) -> web.Response:
        guild, _, _ = await self._post_guild(request)
        self.bot.scanner.invalidate(guild.id)
        self.bot.spawn(self.bot.analyze(guild))
        raise self._redirect(f"/g/{guild.id}", ok="Rescanning.")

    async def clear_data(self, request: web.Request) -> web.Response:
        guild, _, _ = await self._post_guild(request)
        removed = self.bot.storage.forget(guild.id)
        self.bot.bump_history(guild.id)
        raise self._redirect(f"/g/{guild.id}", ok=f"Deleted {removed} stored messages.")
