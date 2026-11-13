"""The owner web dashboard: every server SYKE is in, their channels and chats, and controls.

Locked to SYKE_OWNER_IDS through Discord OAuth. Runs in the bot's own event loop, so handlers can
touch discord.py objects directly. Turned on only when a client id, secret and session secret are set
(see Settings.web_enabled); otherwise the bot runs exactly as before.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
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
MEDIA_SCAN = 200
BRAND = "#8B5CF6"

# Filters the sidebar channel list as you type, hiding empty category headers. Shared by the
# chat and media views (both carry the sidebar).
SIDEBAR_JS = (
    "var _s=document.getElementById('chsearch');"
    "if(_s){_s.addEventListener('input',function(){var q=_s.value.toLowerCase();"
    "document.querySelectorAll('#chlist .dc-ch').forEach(function(el){"
    "el.style.display=el.textContent.toLowerCase().indexOf(q)>-1?'':'none';});"
    "document.querySelectorAll('#chlist .dc-cat').forEach(function(c){var n=c.nextElementSibling,a=false;"
    "while(n&&!n.classList.contains('dc-cat')){if(n.classList.contains('dc-ch')&&n.style.display!=='none')a=true;"
    "n=n.nextElementSibling;}c.style.display=a?'':'none';});});}"
)


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _role_pos(member) -> int:
    return getattr(getattr(member, "top_role", None), "position", 0) or 0


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
/* Discord-like channel view */
.dc{display:flex;height:calc(100vh - 56px);}
.dc-side{width:240px;flex:0 0 240px;background:#17171d;border-right:1px solid var(--line);overflow-y:auto;padding:8px 6px;}
.dc-side .srv{font-weight:700;padding:6px 8px;border-bottom:1px solid var(--line);margin-bottom:6px;display:block;color:var(--text);}
.dc-cat{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:12px 8px 2px;font-weight:700;}
.dc-ch{display:block;padding:5px 8px;border-radius:6px;color:#9aa0b3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px;}
.dc-ch:hover{background:#22222c;color:var(--text);text-decoration:none;}
.dc-ch.on{background:#2d2d3a;color:#fff;}
.dc-ch.voice{color:#5c5f70;cursor:default;}
.dc-ch.voice:hover{background:none;color:#5c5f70;}
.dc-main{flex:1;display:flex;flex-direction:column;min-width:0;}
.dc-head{padding:12px 16px;border-bottom:1px solid var(--line);font-weight:700;display:flex;gap:10px;align-items:center;background:#1a1a22;}
.dc-head .hash{color:var(--muted);}
.dc-head .topic{font-weight:400;color:var(--muted);font-size:13px;border-left:1px solid var(--line);padding-left:10px;}
.dc-msgs{flex:1;overflow-y:auto;padding:14px 16px 20px;display:flex;flex-direction:column;}
.m{display:flex;gap:14px;padding:2px 8px;border-radius:4px;margin-top:14px;}
.m.grouped{margin-top:0;}
.m:hover{background:#1e1e26;}
.m .av{width:40px;flex:0 0 40px;}
.m .av img{width:40px;height:40px;border-radius:50%;display:block;}
.m.grouped .av{visibility:hidden;height:0;}
.m .content{min-width:0;flex:1;}
.m .head{display:flex;align-items:baseline;gap:8px;line-height:1.3;}
.m .name{font-weight:600;color:#f2f3f5;}
.m .name .app{background:var(--brand);color:#fff;font-size:9px;font-weight:700;border-radius:3px;padding:0 4px;margin-left:5px;vertical-align:middle;letter-spacing:.02em;}
.m .ts{font-size:11px;color:var(--muted);}
.m .text{white-space:pre-wrap;word-break:break-word;color:#dbdee1;}
.m .text.empty{color:var(--muted);font-style:italic;}
.m .reply{font-size:13px;color:var(--muted);margin-bottom:2px;display:flex;gap:6px;align-items:center;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.m .reply img{width:16px;height:16px;border-radius:50%;}
.m .reply .rn{color:#b9bbbe;font-weight:600;}
.att{display:block;margin-top:4px;}
.att img{max-width:420px;max-height:320px;border-radius:8px;display:block;}
.att-media{max-width:420px;max-height:320px;border-radius:8px;display:block;background:#000;}
.att-audio audio{width:360px;max-width:100%;margin-top:3px;}
.att-audio .fn{font-size:13px;color:#dbdee1;}
.att-file{display:flex;gap:10px;align-items:center;background:#2b2d31;border:1px solid var(--line);
border-radius:8px;padding:10px 12px;max-width:400px;color:var(--text);}
.att-file:hover{text-decoration:none;border-color:var(--brand);}
.att-file .fi{font-size:20px;}
.att-file .fn{font-size:13px;word-break:break-word;}
.emb{border-left:4px solid var(--brand);background:#2b2d31;border-radius:4px;padding:8px 12px;margin-top:4px;max-width:460px;}
.emb .et{font-weight:600;margin-bottom:2px;display:block;color:#8ab4f8;}
.emb .ed{color:#dbdee1;font-size:14px;}
.emb .emb-img{display:block;max-width:100%;max-height:300px;border-radius:6px;margin-top:8px;}
.rx{display:inline-flex;gap:5px;align-items:center;background:#2b2d31;border:1px solid var(--line);border-radius:8px;padding:1px 7px;font-size:12px;margin:5px 4px 0 0;color:#b9bbbe;}
.dc-input{padding:10px 16px 14px;border-top:1px solid var(--line);background:#1a1a22;}
.dc-input textarea{width:100%;resize:vertical;background:#2b2d31;border-color:#2b2d31;}
.dc-input .ir{margin-top:6px;display:flex;justify-content:space-between;align-items:center;}
.dc-empty{color:var(--muted);text-align:center;margin-top:30px;}
.chsearch{width:calc(100% - 4px);margin:4px 2px 6px;padding:6px 8px;background:#0f0f14;border:1px solid var(--line);
border-radius:6px;color:var(--text);font-size:13px;}
.ch-tabs{display:flex;gap:4px;margin-left:auto;}
.ch-tabs a{font-size:13px;font-weight:600;color:var(--muted);padding:4px 10px;border-radius:6px;}
.ch-tabs a:hover{background:#22222c;color:var(--text);text-decoration:none;}
.ch-tabs a.on{background:#2d2d3a;color:#fff;}
.media-grid{flex:1;overflow-y:auto;padding:14px 16px;display:grid;gap:8px;align-content:start;
grid-template-columns:repeat(auto-fill,minmax(150px,1fr));}
.tile{position:relative;aspect-ratio:1;border-radius:8px;overflow:hidden;background:#000;display:block;}
.tile img,.tile video{width:100%;height:100%;object-fit:cover;display:block;}
.tile .cap{position:absolute;left:0;right:0;bottom:0;padding:4px 6px;font-size:11px;color:#fff;
background:linear-gradient(transparent,rgba(0,0,0,.8));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tile .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:30px;
color:#fff;text-shadow:0 1px 4px #000;pointer-events:none;}
.tile.file{aspect-ratio:auto;background:#2b2d31;padding:10px;color:var(--text);font-size:13px;}
.ch-search{margin-left:auto;}
.ch-search input{background:#0f0f14;border:1px solid var(--line);border-radius:6px;color:var(--text);
font-size:13px;padding:5px 8px;width:200px;}
.sr-head{color:var(--muted);font-size:13px;padding:4px 2px 10px;border-bottom:1px solid var(--line);margin-bottom:8px;
display:flex;justify-content:space-between;align-items:center;}
.sr{padding:8px;border-radius:6px;margin-bottom:2px;}
.sr:hover{background:#1e1e26;}
.sr .head{display:flex;align-items:baseline;gap:8px;}
.sr .name{font-weight:600;color:#f2f3f5;}
.sr .jump{margin-left:auto;font-size:12px;}
.sr .text{white-space:pre-wrap;word-break:break-word;color:#dbdee1;margin-top:2px;}
mark{background:#f5b30155;color:#ffe08a;border-radius:2px;padding:0 1px;}
.m .name{text-decoration:none;}
.m .name:hover{text-decoration:underline;}
.m .av img{cursor:pointer;}
/* Member list (right column) */
.dc-members{width:240px;flex:0 0 240px;background:#17171d;border-left:1px solid var(--line);overflow-y:auto;padding:12px 8px;}
.dc-members .mcat{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:700;margin:12px 8px 4px;}
.mrow{display:flex;gap:10px;align-items:center;padding:4px 8px;border-radius:6px;color:#c6c9d2;}
.mrow:hover{background:#22222c;text-decoration:none;color:#fff;}
.mrow img{width:28px;height:28px;border-radius:50%;}
.mrow .mn{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px;}
.mrow .mbot{font-size:8px;background:var(--brand);color:#fff;border-radius:3px;padding:0 3px;margin-left:4px;}
@media(max-width:900px){.dc-members{display:none;}}
/* Profile page */
.prof{max-width:720px;margin:0 auto;padding:24px 20px;}
.prof-top{display:flex;gap:18px;align-items:center;}
.prof-top img{width:96px;height:96px;border-radius:50%;}
.prof-name{font-size:24px;font-weight:700;}
.prof-user{color:var(--muted);}
.prof-roles{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;}
.prof-role{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:2px 10px;display:flex;align-items:center;gap:5px;}
.prof-role .dot{width:8px;height:8px;border-radius:50%;}
.prof-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px;}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;}
.stat .k{color:var(--muted);font-size:12px;}
.stat .v{font-size:20px;font-weight:700;margin-top:2px;}
.prof-sec{margin-top:22px;}
.prof-sec h3{font-size:15px;margin:0 0 8px;}
.bar{height:8px;border-radius:6px;background:var(--panel2);overflow:hidden;margin:4px 0 8px;}
.bar > i{display:block;height:100%;background:var(--brand);}
.emoji-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;}
.emoji{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 6px;text-align:center;}
.emoji img{width:44px;height:44px;object-fit:contain;}
.emoji .en{display:block;margin-top:6px;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.emoji .tag{font-size:9px;color:var(--brand);}
"""


def layout(title: str, body: str, *, user: Optional[tuple[int, str]] = None, wide: bool = False,
           head_extra: str = "") -> web.Response:
    nav = ""
    if user is not None:
        nav = (f'<span class="muted">{esc(user[1])}</span>'
               f'<a class="btn sm" href="/logout">Log out</a>')
    inner = body if wide else f"<div class='wrap'>{body}</div>"
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · SYKE</title><style>{STYLE}</style>{head_extra}</head><body>"
        f"<header><span class='brand'>SYKE</span> <a class='btn sm' href='/'>Servers</a>"
        f"<span class='sp'></span>{nav}</header>{inner}</body></html>"
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
        a("/g/{gid}/emojis", self.server_emojis)
        a("/g/{gid}/u/{uid}", self.member_profile)
        a("/g/{gid}/c/{cid}", self.channel)
        a("/g/{gid}/c/{cid}/messages", self.channel_messages)
        a("/g/{gid}/c/{cid}/media", self.channel_media)
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
        emoji_count = len(getattr(guild, "emojis", ()))
        body = (
            f"{flash(request)}<h1>{esc(guild.name)}</h1>"
            f"<div class='muted'>{guild.member_count or 0} members · owner "
            f"{esc(getattr(guild.owner, 'display_name', guild.owner_id))} · "
            f"<a href='/g/{guild.id}/emojis'>{emoji_count} emojis</a></div>"
            f"<div class='grid' style='margin-top:14px'>{settings_form}{mod}{danger}</div>"
            f"<h2>Channels</h2><table>{''.join(rows) or '<tr><td>No text channels</td></tr>'}</table>"
        )
        return layout(guild.name, body, user=user)

    async def server_emojis(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        emojis = sorted(getattr(guild, "emojis", ()), key=lambda e: (e.animated, e.name.lower()))
        tiles = []
        for e in emojis:
            tag = "<span class='tag'>GIF</span>" if e.animated else ""
            tiles.append(f"<div class='emoji'><a href='{esc(e.url)}' target='_blank' rel='noreferrer'>"
                         f"<img src='{esc(e.url)}' alt='{esc(e.name)}' loading='lazy'></a>"
                         f"<span class='en'>:{esc(e.name)}: {tag}</span></div>")
        stickers = getattr(guild, "stickers", ())
        sticker_html = ""
        if stickers:
            scards = []
            for s in stickers:
                url = getattr(s, "url", "")
                scards.append(f"<div class='emoji'><a href='{esc(url)}' target='_blank' rel='noreferrer'>"
                              f"<img src='{esc(url)}' alt='{esc(s.name)}' loading='lazy'></a>"
                              f"<span class='en'>{esc(s.name)}</span></div>")
            sticker_html = f"<h2>Stickers ({len(stickers)})</h2><div class='emoji-grid'>{''.join(scards)}</div>"
        grid = f"<div class='emoji-grid'>{''.join(tiles)}</div>" if tiles else \
            "<div class='dc-empty'>This server has no custom emojis.</div>"
        body = (f"<div class='muted'><a href='/g/{guild.id}'>← {esc(guild.name)}</a></div>"
                f"<h1>Emojis ({len(emojis)})</h1>{grid}{sticker_html}")
        return layout(f"{guild.name} emojis", body, user=user)

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

    def _channel_or_404(self, request: web.Request, guild: discord.Guild):
        try:
            cid = int(request.match_info["cid"])
        except ValueError:
            raise web.HTTPNotFound(text="bad channel id")
        channel = guild.get_channel_or_thread(cid) if hasattr(guild, "get_channel_or_thread") else guild.get_channel(cid)
        if channel is None or not hasattr(channel, "history") or not hasattr(channel, "send"):
            raise web.HTTPNotFound(text="no such channel")
        return channel

    def _sidebar(self, guild: discord.Guild, active_id: int) -> str:
        """Channel list grouped by category, Discord-style, with a search box."""
        rows = [f"<a class='srv' href='/g/{guild.id}'>← {esc(guild.name)}</a>",
                "<input id='chsearch' class='chsearch' placeholder='Search channels' autocomplete='off'>",
                "<div id='chlist'>"]
        try:
            grouped = guild.by_category()
        except Exception:
            grouped = [(None, list(guild.channels))]
        for category, channels in grouped:
            visible = [c for c in channels if not isinstance(c, discord.CategoryChannel)]
            if not visible:
                continue
            if category is not None:
                rows.append(f"<div class='dc-cat'>{esc(category.name)}</div>")
            for ch in visible:
                if isinstance(ch, discord.VoiceChannel):
                    rows.append(f"<div class='dc-ch voice'>🔊 {esc(ch.name)}</div>")
                    continue
                can = ch.permissions_for(guild.me).read_message_history
                on = " on" if ch.id == active_id else ""
                icon = "🧵" if isinstance(ch, discord.Thread) else "#"
                if can:
                    rows.append(f"<a class='dc-ch{on}' href='/g/{guild.id}/c/{ch.id}'>{icon} {esc(ch.name)}</a>")
                else:
                    rows.append(f"<div class='dc-ch voice' title='SYKE has no access'>🔒 {esc(ch.name)}</div>")
        rows.append("</div>")
        return f"<div class='dc-side'>{''.join(rows)}</div>"

    def _channel_header(self, guild: discord.Guild, channel, active: str, query: str = "") -> str:
        topic = getattr(channel, "topic", None)
        topic_html = f"<span class='topic'>{esc(topic[:120])}</span>" if topic else ""
        base = f"/g/{guild.id}/c/{channel.id}"
        search = (f"<form class='ch-search' method='get' action='{base}'>"
                  f"<input name='q' value='{esc(query)}' placeholder='Search messages' autocomplete='off'></form>")
        tabs = (f"<div class='ch-tabs'>"
                f"<a class='{'on' if active == 'chat' else ''}' href='{base}'>Chat</a>"
                f"<a class='{'on' if active == 'media' else ''}' href='{base}/media'>Media</a></div>")
        return (f"<div class='dc-head'><span class='hash'>#</span> {esc(channel.name)}{topic_html}{search}{tabs}</div>")

    def _highlight(self, text: str, query: str) -> str:
        safe = esc(text)
        if not query:
            return safe
        try:
            pattern = re.compile(re.escape(esc(query)), re.IGNORECASE)
        except re.error:
            return safe
        return pattern.sub(lambda mo: f"<mark>{mo.group(0)}</mark>", safe)

    async def _search_html(self, guild: discord.Guild, channel, query: str) -> str:
        rows = await asyncio.to_thread(self.bot.storage.search_channel, guild.id, channel.id, query, 80)
        results = [(r[0], r[2] or f"user {r[1]}", r[3], r[4]) for r in rows]
        note = ""
        if not results and channel.permissions_for(guild.me).read_message_history:
            # Not a stored channel (or nothing stored) — scan recent live history instead.
            note = "<div class='note'>Searching recent messages (this channel isn't fully stored).</div>"
            needle = query.lower()
            try:
                async for m in channel.history(limit=MEDIA_SCAN):
                    if needle in (m.clean_content or "").lower():
                        results.append((m.id, m.author.display_name, m.clean_content, m.created_at.timestamp()))
                        if len(results) >= 80:
                            break
            except discord.HTTPException:
                pass
        clear = f"<a href='/g/{guild.id}/c/{channel.id}'>Clear</a>"
        head = (f"<div class='sr-head'><span>{len(results)} result(s) for "
                f"<strong>{esc(query)}</strong></span>{clear}</div>")
        if not results:
            return head + "<div class='dc-empty'>No messages matched.</div>"
        items = []
        for mid, name, content, created in results:
            ts = f"<time class='ts' data-ts='{int(created)}'></time>" if created else ""
            jump = (f"<a class='jump' href='https://discord.com/channels/{guild.id}/{channel.id}/{mid}' "
                    f"target='_blank' rel='noreferrer'>Jump ↗</a>") if mid else ""
            items.append(f"<div class='sr'><div class='head'><span class='name'>{esc(name)}</span>{ts}{jump}</div>"
                         f"<div class='text'>{self._highlight(content, query)}</div></div>")
        return head + note + "".join(items)

    def _one_message(self, m: discord.Message, prev: discord.Message | None) -> str:
        author = m.author
        same = (prev is not None and prev.author.id == author.id
                and (m.created_at - prev.created_at).total_seconds() < 420
                and m.reference is None and prev.reference is None)
        avatar = getattr(getattr(author, "display_avatar", None), "url", "")
        colour = getattr(getattr(author, "colour", None), "value", 0)
        name_style = f" style='color:#{colour:06x}'" if colour else ""
        app = "<span class='app'>APP</span>" if getattr(author, "bot", False) else ""
        ts = int(m.created_at.timestamp())
        reply = ""
        if m.reference is not None:
            ref = m.reference.resolved
            if isinstance(ref, discord.Message):
                ref_av = getattr(getattr(ref.author, "display_avatar", None), "url", "")
                snippet = " ".join((ref.clean_content or "").split())[:80] or "(attachment)"
                reply = (f"<div class='reply'>↳ <img src='{esc(ref_av)}' alt=''>"
                         f"<span class='rn'>{esc(ref.author.display_name)}</span> {esc(snippet)}</div>")
            else:
                reply = "<div class='reply'>↳ replying to a message</div>"
        text = esc(m.clean_content)
        body = f"<div class='text'>{text}</div>" if text else "<div class='text empty'>(no text content)</div>"
        atts = []
        for a in m.attachments:
            ct = (a.content_type or "").lower()
            url = esc(a.url)
            if ct.startswith("image"):
                atts.append(f"<a class='att' href='{url}' target='_blank' rel='noreferrer'>"
                            f"<img src='{url}' alt='{esc(a.filename)}' loading='lazy'></a>")
            elif ct.startswith("video"):
                atts.append(f"<div class='att'><video class='att-media' src='{url}' controls "
                            f"preload='metadata'></video></div>")
            elif ct.startswith("audio"):
                atts.append(f"<div class='att att-audio'><div class='fn'>🎵 {esc(a.filename)}</div>"
                            f"<audio src='{url}' controls preload='none'></audio></div>")
            else:
                kb = f"{a.size // 1024} KB" if getattr(a, "size", 0) else "file"
                atts.append(f"<a class='att att-file' href='{url}' target='_blank' rel='noreferrer'>"
                            f"<span class='fi'>📎</span><span class='fn'>{esc(a.filename)}"
                            f"<br><span class='muted'>{kb}</span></span></a>")
        embeds = []
        for e in m.embeds:
            link = getattr(e, "url", None)
            title = esc(e.title)
            if title:
                title = (f"<a class='et' href='{esc(link)}' target='_blank' rel='noreferrer'>{title}</a>"
                         if link else f"<div class='et'>{title}</div>")
            desc = f"<div class='ed'>{esc(e.description[:600])}</div>" if e.description else ""
            media = ""
            video_url = getattr(getattr(e, "video", None), "url", None)
            image_url = getattr(getattr(e, "image", None), "url", None)
            thumb_url = getattr(getattr(e, "thumbnail", None), "url", None)
            if getattr(e, "type", "") == "gifv" and video_url:
                media = (f"<video class='emb-img' src='{esc(video_url)}' autoplay loop muted "
                         f"playsinline preload='metadata'></video>")
            elif image_url:
                media = f"<img class='emb-img' src='{esc(image_url)}' loading='lazy' alt=''>"
            elif thumb_url:
                media = f"<img class='emb-img' src='{esc(thumb_url)}' loading='lazy' alt=''>"
            if title or desc or media:
                embeds.append(f"<div class='emb'>{title}{desc}{media}</div>")
        reactions = "".join(
            f"<span class='rx'>{esc(str(r.emoji))} {r.count}</span>" for r in getattr(m, "reactions", [])
        )
        profile = f"/g/{getattr(getattr(m, 'guild', None), 'id', 0)}/u/{author.id}"
        head = ("" if same else
                f"<div class='head'><a class='name' href='{profile}'{name_style}>{esc(author.display_name)}{app}</a>"
                f"<time class='ts' data-ts='{ts}'></time></div>")
        av = f"<a class='av' href='{profile}'><img src='{esc(avatar)}' alt='' loading='lazy'></a>"
        return (f"<div class='m{' grouped' if same else ''}' data-mid='{m.id}'>{av}"
                f"<div class='content'>{reply}{head}{body}{''.join(atts)}{''.join(embeds)}{reactions}</div></div>")

    async def _messages_html(self, channel, before: int | None = None) -> str:
        if not channel.permissions_for(channel.guild.me).read_message_history:
            if before is not None:
                return ""  # Can't page a channel we can't read.
            stored = self.bot.storage.corpus(channel.guild.id)[:STORED_PREVIEW]
            if not stored:
                return "<div class='dc-empty'>SYKE can't read this channel and has nothing stored from it.</div>"
            rows = ["<div class='note'>SYKE can't read live history here — showing stored messages for the server.</div>"]
            for author_id, content in stored:
                rows.append(f"<div class='m'><div class='av'></div><div class='content'>"
                            f"<div class='head'><span class='name'>user {author_id}</span></div>"
                            f"<div class='text'>{esc(content)}</div></div></div>")
            return "".join(rows)
        kwargs = {"limit": MESSAGES_SHOWN}
        if before is not None:
            kwargs["before"] = discord.Object(id=before)
        try:
            collected = [m async for m in channel.history(**kwargs)]
        except discord.HTTPException:
            return "" if before is not None else "<div class='note err'>Couldn't read live history just now.</div>"
        collected.reverse()
        if not collected:
            return "" if before is not None else "<div class='dc-empty'>No messages yet.</div>"
        html = []
        prev = None
        for m in collected:
            html.append(self._one_message(m, prev))
            prev = m
        return "".join(html)

    async def channel(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        channel = self._channel_or_404(request, guild)
        can_send = channel.permissions_for(guild.me).send_messages
        sidebar = self._sidebar(guild, channel.id)
        query = (request.query.get("q") or "").strip()
        searching = bool(query)
        messages = await self._search_html(guild, channel, query) if searching \
            else await self._messages_html(channel)
        csrf = self._hidden_csrf(user[0])
        head = self._channel_header(guild, channel, "chat", query)
        if can_send:
            inp = (f"<form class='dc-input' method='post' action='/g/{guild.id}/c/{channel.id}/say'>{csrf}"
                   f"<textarea name='content' rows='2' maxlength='2000' required "
                   f"placeholder='Message #{esc(channel.name)} as SYKE…'></textarea>"
                   f"<div class='ir'><span class='muted'>Posts as SYKE</span>"
                   f"<button class='btn brand'>Send</button></div></form>")
        else:
            inp = "<div class='dc-input'><div class='note'>SYKE can't send messages in this channel.</div></div>"
        flash_html = flash(request)
        members = self._member_list(guild)
        body = (
            f"<div class='dc'>{sidebar}<div class='dc-main'>{head}"
            f"<div class='dc-msgs' id='messages'>{flash_html}{messages}</div>{inp}</div>{members}</div>"
            "<script>"
            "function fmt(s){var d=new Date(s*1000),n=new Date(),"
            "t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),"
            "y=new Date(n.getTime()-864e5);"
            "if(d.toDateString()===n.toDateString())return'Today at '+t;"
            "if(d.toDateString()===y.toDateString())return'Yesterday at '+t;"
            "return d.toLocaleDateString()+' '+t;}"
            "function stamp(r){r.querySelectorAll('time[data-ts]').forEach(function(e){e.textContent=fmt(+e.dataset.ts);});}"
            "var pane=document.getElementById('messages');"
            "function bot(){return pane.scrollHeight-pane.scrollTop-pane.clientHeight<100;}"
            "stamp(document);"
            + ("" if searching else
               "pane.scrollTop=pane.scrollHeight;"
               # Live refresh, but only while you're already at the bottom — never yanks you up.
               "async function poll(){if(!bot())return;try{var r=await fetch(location.pathname+'/messages');"
               "if(!r.ok)return;var h=await r.text();pane.innerHTML=h;stamp(pane);pane.scrollTop=pane.scrollHeight;}"
               "catch(e){}}setInterval(poll,8000);"
               # Scroll up to load older history, Discord-style, keeping your place.
               "var loading=false,noMore=false;"
               "async function older(){if(loading||noMore)return;var f=pane.querySelector('.m[data-mid]');if(!f)return;"
               "loading=true;try{var r=await fetch(location.pathname+'/messages?before='+f.dataset.mid);"
               "if(r.ok){var h=await r.text();if(h.indexOf('data-mid')===-1){noMore=true;}else{"
               "var before=pane.scrollHeight;f.insertAdjacentHTML('beforebegin',h);stamp(pane);"
               "pane.scrollTop+=pane.scrollHeight-before;}}}catch(e){}loading=false;}"
               "pane.addEventListener('scroll',function(){if(pane.scrollTop<150)older();});")
            + SIDEBAR_JS +
            "</script>"
        )
        return layout(f"#{channel.name}", body, user=user, wide=True)

    def _member_list(self, guild: discord.Guild, limit: int = 200) -> str:
        """Right-hand member column, grouped by top role, Discord-style. Best-effort from cache."""
        raw = getattr(guild, "members", [])
        members = [m for m in (raw.values() if isinstance(raw, dict) else raw) if m is not None]
        if not members:
            return "<div class='dc-members'><div class='mcat'>Members</div>" \
                   "<div class='muted' style='padding:6px 8px'>Member list needs the Server Members intent.</div></div>"
        members = sorted(members, key=lambda m: (-_role_pos(m), (m.display_name or "").lower()))[:limit]
        groups: dict[str, list] = {}
        order: list[str] = []
        for m in members:
            role = getattr(m, "top_role", None)
            label = role.name if role is not None and not role.is_default() else "Members"
            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(m)
        rows = [f"<div class='dc-members'><div class='mcat'>Members — {len(members)}</div>"]
        for label in order:
            rows.append(f"<div class='mcat'>{esc(label)} — {len(groups[label])}</div>")
            for m in groups[label]:
                avatar = getattr(getattr(m, "display_avatar", None), "url", "")
                colour = getattr(getattr(m, "colour", None), "value", 0)
                style = f" style='color:#{colour:06x}'" if colour else ""
                bot = "<span class='mbot'>APP</span>" if getattr(m, "bot", False) else ""
                rows.append(f"<a class='mrow' href='/g/{guild.id}/u/{m.id}'>"
                            f"<img src='{esc(avatar)}' alt='' loading='lazy'>"
                            f"<span class='mn'{style}>{esc(m.display_name)}{bot}</span></a>")
        rows.append("</div>")
        return "".join(rows)

    async def member_profile(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            raise web.HTTPNotFound(text="bad user id")
        member = guild.get_member(uid)
        if member is None:
            with contextlib.suppress(discord.HTTPException):
                member = await guild.fetch_member(uid)
        body = self._profile_body(guild, uid, member, user[0])
        name = getattr(member, "display_name", None) or f"User {uid}"
        return layout(name, body, user=user)

    def _profile_body(self, guild: discord.Guild, uid: int, member, owner_id: int) -> str:
        storage = self.bot.storage
        count, first, last, stored_name = storage.member_stats(guild.id, uid)
        cases = storage.cases(guild.id, uid)
        now = time.time()
        restrictions = [r for r in storage.restrictions(guild.id, uid) if r[2] is None or r[2] > now]
        name = getattr(member, "display_name", None) or stored_name or f"User {uid}"
        username = getattr(member, "name", None)
        avatar = getattr(getattr(member, "display_avatar", None), "url", "")
        header_img = f"<img src='{esc(avatar)}' alt=''>" if avatar else ""
        roles_html = ""
        if member is not None and hasattr(member, "roles"):
            chips = []
            for r in sorted([r for r in member.roles if not r.is_default()],
                            key=lambda r: r.position, reverse=True):
                colour = r.colour.value or 0x99AAB5
                chips.append(f"<span class='prof-role'><span class='dot' style='background:#{colour:06x}'></span>"
                             f"{esc(r.name)}</span>")
            roles_html = f"<div class='prof-roles'>{''.join(chips)}</div>" if chips else ""
        created = getattr(member, "created_at", None)
        joined = getattr(member, "joined_at", None)

        def when(dt):
            return f"<time data-ts='{int(dt.timestamp())}'></time>" if dt else "—"

        stats = [
            ("Stored messages", f"{count:,}"),
            ("First seen", when(datetime.fromtimestamp(first, timezone.utc)) if first else "—"),
            ("Last seen", when(datetime.fromtimestamp(last, timezone.utc)) if last else "—"),
            ("Joined server", when(joined)),
            ("Account made", when(created)),
            ("Mod cases", str(len(cases))),
        ]
        stat_html = "".join(f"<div class='stat'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
                            for k, v in stats)
        top = storage.member_top_channels(guild.id, uid)
        chan_html = ""
        if top:
            items = []
            for cid, n in top:
                ch = guild.get_channel(cid)
                where = f"<a href='/g/{guild.id}/c/{cid}'>#{esc(ch.name)}</a>" if ch else f"channel {cid}"
                items.append(f"<li>{where} — {n:,} messages</li>")
            chan_html = f"<div class='prof-sec'><h3>Most active in</h3><ul>{''.join(items)}</ul></div>"
        restr_html = ""
        if restrictions:
            parts = []
            for _, kind, ends, *_ in restrictions:
                parts.append(f"{esc(kind)}" + (f" (ends <time data-ts='{int(ends)}'></time>)" if ends else " (permanent)"))
            restr_html = f"<div class='prof-sec'><h3>Active restrictions</h3><p>{', '.join(parts)}</p></div>"
        hist_html = "<div class='prof-sec'><h3>Moderation history</h3>"
        if cases:
            lines = []
            for number, mod_id, action, reason, duration, created_at in cases[:20]:
                dur = f" · {human_duration(duration)}" if duration else ""
                lines.append(f"<li><code>#{number}</code> <strong>{esc(action)}</strong>{dur} · "
                             f"<time data-ts='{int(created_at)}'></time> by &lt;@{mod_id}&gt; — "
                             f"{esc(reason or 'no reason')}</li>")
            hist_html += f"<ul>{''.join(lines)}</ul>"
            if len(cases) > 20:
                hist_html += f"<p class='muted'>…and {len(cases) - 20} older</p>"
        else:
            hist_html += "<p class='muted'>Clean record.</p>"
        hist_html += "</div>"
        actions = ""
        if member is not None:
            csrf = self._hidden_csrf(owner_id)
            actions = (
                f"<div class='prof-sec'><h3>Actions</h3>"
                f"<form method='post' action='/g/{guild.id}/mod' class='card'>{csrf}"
                f"<input type='hidden' name='user_id' value='{uid}'>"
                f"<div class='row'><select name='action'>"
                f"<option value='timeout'>Timeout</option><option value='warn'>Warn</option>"
                f"<option value='restrict'>Restrict</option><option value='kick'>Kick</option>"
                f"<option value='ban'>Ban</option><option value='untimeout'>Untimeout</option>"
                f"<option value='unrestrict'>Unrestrict</option></select>"
                f"<input name='duration' placeholder='10m (timeout/restrict)' style='max-width:180px'>"
                f"<input name='reason' placeholder='Reason'>"
                f"<button class='btn brand'>Apply</button></div></form></div>"
            )
        gone = "" if member is not None else "<div class='note'>Not in the server right now — showing stored data.</div>"
        handle = f"<div class='prof-user'>@{esc(username)}</div>" if username else ""
        back = f"<div class='muted'><a href='/g/{guild.id}'>← {esc(guild.name)}</a></div>"
        return (
            f"<div class='prof'>{back}{gone}"
            f"<div class='prof-top'>{header_img}<div><div class='prof-name'>{esc(name)}</div>{handle}"
            f"<div class='muted'>ID {uid}</div>{roles_html}</div></div>"
            f"<div class='prof-grid'>{stat_html}</div>{chan_html}{restr_html}{hist_html}{actions}</div>"
            "<script>document.querySelectorAll('time[data-ts]').forEach(function(e){"
            "var d=new Date(+e.dataset.ts*1000);e.textContent=d.toLocaleDateString()+' '+"
            "d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});});</script>"
        )

    async def channel_media(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        channel = self._channel_or_404(request, guild)
        sidebar = self._sidebar(guild, channel.id)
        grid = await self._media_html(channel)
        head = self._channel_header(guild, channel, "media")
        body = (
            f"<div class='dc'>{sidebar}<div class='dc-main'>{head}{grid}</div></div>"
            "<script>function fmt(s){var d=new Date(s*1000),n=new Date(),"
            "t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),y=new Date(n.getTime()-864e5);"
            "if(d.toDateString()===n.toDateString())return'Today '+t;"
            "if(d.toDateString()===y.toDateString())return'Yesterday '+t;return d.toLocaleDateString();}"
            "document.querySelectorAll('time[data-ts]').forEach(function(e){e.textContent=fmt(+e.dataset.ts);});"
            + SIDEBAR_JS + "</script>"
        )
        return layout(f"#{channel.name} · media", body, user=user, wide=True)

    async def _media_html(self, channel) -> str:
        if not channel.permissions_for(channel.guild.me).read_message_history:
            return "<div class='dc-empty'>SYKE can't read this channel, so it can't show its media.</div>"
        try:
            tiles = []
            async for m in channel.history(limit=MEDIA_SCAN):
                ts = int(m.created_at.timestamp())
                who = esc(m.author.display_name)
                for a in m.attachments:
                    ct = a.content_type or ""
                    cap = f"<span class='cap'>{who} · <time data-ts='{ts}'></time></span>"
                    if ct.startswith("image"):
                        tiles.append(f"<a class='tile' href='{esc(a.url)}' target='_blank' rel='noreferrer'>"
                                     f"<img src='{esc(a.url)}' loading='lazy' alt=''>{cap}</a>")
                    elif ct.startswith("video"):
                        tiles.append(f"<a class='tile' href='{esc(a.url)}' target='_blank' rel='noreferrer'>"
                                     f"<video src='{esc(a.url)}#t=0.1' preload='metadata' muted></video>"
                                     f"<span class='play'>▶</span>{cap}</a>")
                    else:
                        kb = f"{a.size // 1024} KB" if getattr(a, "size", 0) else ""
                        tiles.append(f"<a class='tile file' href='{esc(a.url)}' target='_blank' rel='noreferrer'>"
                                     f"📎 {esc(a.filename)}<br><span class='muted'>{kb} · {who}</span></a>")
                for e in m.embeds:
                    url = getattr(getattr(e, "thumbnail", None), "url", None) or getattr(getattr(e, "image", None), "url", None)
                    if url:
                        tiles.append(f"<a class='tile' href='{esc(getattr(e, 'url', None) or url)}' target='_blank' "
                                     f"rel='noreferrer'><img src='{esc(url)}' loading='lazy' alt=''>"
                                     f"<span class='cap'>{who} · <time data-ts='{ts}'></time></span></a>")
        except discord.HTTPException:
            return "<div class='dc-empty'>Couldn't load media just now.</div>"
        if not tiles:
            return (f"<div class='dc-empty'>No media in the last {MEDIA_SCAN} messages of this channel.</div>")
        return f"<div class='media-grid'>{''.join(tiles)}</div>"

    async def channel_messages(self, request: web.Request) -> web.Response:
        """Message list HTML. ?before=<message_id> pages older history; otherwise the latest."""
        user = self._user(request)
        if user is None:
            raise web.HTTPForbidden(text="login required")
        guild = self._guild_or_404(request)
        channel = self._channel_or_404(request, guild)
        before_raw = request.query.get("before")
        before = int(before_raw) if before_raw and before_raw.isdigit() else None
        return web.Response(text=await self._messages_html(channel, before), content_type="text/html")

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
