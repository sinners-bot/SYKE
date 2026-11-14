"""The owner web dashboard: every server SYKE is in, their channels and chats, and controls.

Locked to SYKE_OWNER_IDS through Discord OAuth. Runs in the bot's own event loop, so handlers can
touch discord.py objects directly. Turned on only when a client id, secret and session secret are set
(see Settings.web_enabled); otherwise the bot runs exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from collections import defaultdict
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


_TOKEN = re.compile(r"<a?:\w{2,32}:\d+>|<@!?\d+>|<#\d+>|<@&\d+>|@everyone|@here")
_EMOJI = re.compile(r"<(a)?:(\w{2,32}):(\d+)>")


def _token_html(token: str, guild, names: dict) -> str:
    em = _EMOJI.match(token)
    if em:
        ext = "gif" if em.group(1) else "png"
        name = em.group(2)
        return (f"<img class='ie' src='https://cdn.discordapp.com/emojis/{em.group(3)}.{ext}' "
                f"alt=':{esc(name)}:' title=':{esc(name)}:' loading='lazy'>")
    if token in ("@everyone", "@here"):
        return f"<span class='mention'>{token}</span>"
    user = re.match(r"<@!?(\d+)>", token)
    if user:
        uid = int(user.group(1))
        member = guild.get_member(uid) if guild is not None and hasattr(guild, "get_member") else None
        # The message payload carries mentioned users' names even without the Members intent.
        name = names.get(("u", uid)) or getattr(member, "display_name", None) or uid
        return f"<span class='mention'>@{esc(name)}</span>"
    chan = re.match(r"<#(\d+)>", token)
    if chan:
        cid = int(chan.group(1))
        ch = guild.get_channel(cid) if guild is not None and hasattr(guild, "get_channel") else None
        return f"<span class='mention'>#{esc(names.get(('c', cid)) or getattr(ch, 'name', None) or cid)}</span>"
    role = re.match(r"<@&(\d+)>", token)
    if role:
        rid = int(role.group(1))
        r = guild.get_role(rid) if guild is not None and hasattr(guild, "get_role") else None
        return f"<span class='mention'>@{esc(names.get(('r', rid)) or getattr(r, 'name', None) or rid)}</span>"
    return esc(token)


def render_content(content: str, guild, names: dict | None = None) -> str:
    """Escape message text and render custom emojis as images and mentions as chips, Discord-style.

    `names` maps ('u'|'c'|'r', id) -> display name, built from the message's own mention lists so
    usernames resolve even when SYKE doesn't have the Server Members intent.
    """
    if not content:
        return ""
    names = names or {}
    out, last = [], 0
    for mo in _TOKEN.finditer(content):
        out.append(esc(content[last:mo.start()]))
        out.append(_token_html(mo.group(0), guild, names))
        last = mo.end()
    out.append(esc(content[last:]))
    return "".join(out)


def _mention_names(message) -> dict:
    """Resolve names for everyone the message mentions, from the payload (no Members intent needed)."""
    names: dict = {}
    for u in getattr(message, "mentions", []) or []:
        names[("u", u.id)] = getattr(u, "display_name", None) or getattr(u, "name", None) or u.id
    for c in getattr(message, "channel_mentions", []) or []:
        names[("c", c.id)] = getattr(c, "name", None) or c.id
    for r in getattr(message, "role_mentions", []) or []:
        names[("r", r.id)] = getattr(r, "name", None) or r.id
    return names


def base_url(request: web.Request, settings) -> str:
    if settings.web_base_url:
        return settings.web_base_url
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


ANNOUNCE_TITLE = "✨ What's new in SYKE"
# Public-facing features only — never mention the owner dashboard or owner-only tools.
ANNOUNCE_BODY = (
    "**🛡️ Full moderation suite** — timeouts, warns, kicks, bans, tempban & softban, "
    "purge, slowmode, lock, and server-wide `!restrict`.\n"
    "**🚦 Automod** — word/slur filter, invite & link control, and anti-spam.\n"
    "**🚨 Server security** — anti-nuke and anti-raid protection.\n"
    "**🎉 Community** — weekly awards, `!guess`, custom welcome messages, and `!verify` gating.\n"
    "**🤖 Talk to SYKE** — @mention it anywhere and it replies in the server's own voice.\n"
    "**🧠 Profiles & vibes** — `!profile`, `!iq`, `!aura`, `!ship`, `!top`.\n"
    "**😀 Emojis & stickers** — `!emoji`, `!sticker`, `!steal`.\n"
    "**🔤 Text toys** — `!drunk`, `!scramble`, `!zalgo`, `!uwu`, `!mock`, `!keysmash`.\n"
    "Run `!help` to see everything."
)

BADGE_LABELS = {
    "staff": "Discord Staff", "partner": "Partner", "hypesquad": "HypeSquad Events",
    "bug_hunter": "Bug Hunter", "bug_hunter_level_2": "Bug Hunter Gold",
    "hypesquad_bravery": "HypeSquad Bravery", "hypesquad_brilliance": "HypeSquad Brilliance",
    "hypesquad_balance": "HypeSquad Balance", "early_supporter": "Early Supporter",
    "verified_bot": "Verified Bot", "early_verified_bot_developer": "Early Verified Bot Dev",
    "verified_bot_developer": "Early Verified Bot Dev", "discord_certified_moderator": "Certified Mod",
    "active_developer": "Active Developer", "system": "System", "team_user": "Team User",
    "spammer": "⚠ Flagged: likely spammer", "bot_http_interactions": "HTTP-only Bot",
}

# Guild permissions worth surfacing on a profile, strongest first.
KEY_PERMS = [
    ("administrator", "Administrator"), ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"), ("manage_channels", "Manage Channels"),
    ("ban_members", "Ban"), ("kick_members", "Kick"), ("manage_messages", "Manage Messages"),
    ("moderate_members", "Timeout"), ("manage_webhooks", "Manage Webhooks"),
    ("mention_everyone", "Mention Everyone"), ("manage_emojis_and_stickers", "Manage Expressions"),
]


def rough_age(dt, now: datetime | None = None) -> str:
    """A compact human age like '3y 2mo' or '5 days' for a datetime."""
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    days = max((now - dt).days, 0)
    if days < 1:
        return "today"
    if days < 31:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 365:
        mo = days // 30
        return f"{mo} month{'s' if mo != 1 else ''}"
    yrs, rem = days // 365, (days % 365) // 30
    return f"{yrs}y" + (f" {rem}mo" if rem else "")


def hour_histogram(times, tz) -> list[int]:
    """Count a member's messages into 24 local-hour buckets."""
    hours = [0] * 24
    for t in times:
        try:
            hours[datetime.fromtimestamp(t, tz).hour] += 1
        except (ValueError, OSError, OverflowError):
            continue
    return hours


def peak_window(hours: list[int]) -> tuple[int, int] | None:
    """The busiest 3-hour window (start, end) given an hourly histogram, or None if silent."""
    if not any(hours):
        return None
    best_start, best_sum = 0, -1
    for start in range(24):
        total = sum(hours[(start + i) % 24] for i in range(3))
        if total > best_sum:
            best_start, best_sum = start, total
    return best_start, (best_start + 3) % 24


def join_clusters(joins, window: float = 60, min_size: int = 3) -> list[list[int]]:
    """Group user ids whose joins fall within `window` seconds of each other (raid-join bursts)."""
    timed = sorted(((j[5], j[0]) for j in joins if j[5] is not None), key=lambda x: x[0])
    clusters: list[list[int]] = []
    current: list[int] = []
    last = None
    for ts, uid in timed:
        if last is not None and ts - last <= window:
            current.append(uid)
        else:
            if len(current) >= min_size:
                clusters.append(current)
            current = [uid]
        last = ts
    if len(current) >= min_size:
        clusters.append(current)
    return clusters


SEARCH_SCAN = 1000
HAS_KINDS = {"image", "video", "audio", "file", "link", "url", "embed", "sticker", "gif"}
_URL_IN = re.compile(r"https?://", re.IGNORECASE)
_MENTION_ID = re.compile(r"<@!?(\d+)>$")
_BARE_ID = re.compile(r"(\d{15,25})$")


class SearchFilters:
    """A parsed dashboard query: free text plus Discord-style from:/has:/mentions:/in:/before:/after:."""
    __slots__ = ("text", "from_ids", "from_names", "has", "before", "after",
                 "mention_ids", "mention_names", "in_ids", "in_names")

    def __init__(self):
        self.text = ""
        self.from_ids: set[int] = set()
        self.from_names: list[str] = []
        self.has: set[str] = set()
        self.before: float | None = None
        self.after: float | None = None
        self.mention_ids: set[int] = set()
        self.mention_names: list[str] = []
        self.in_ids: set[int] = set()
        self.in_names: list[str] = []

    @property
    def structural(self) -> bool:
        return bool(self.has or self.before is not None or self.after is not None
                    or self.mention_ids or self.mention_names)

    @property
    def has_from(self) -> bool:
        return bool(self.from_ids or self.from_names)

    @property
    def has_in(self) -> bool:
        return bool(self.in_ids or self.in_names)


def _parse_date(s: str) -> float | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def parse_search(query: str) -> SearchFilters:
    """Parse `from:@u has:image before:2025-01-01 words` into a SearchFilters."""
    f = SearchFilters()
    words = []
    for tok in (query or "").split():
        low = tok.lower()
        if low.startswith("from:") and len(tok) > 5:
            v = tok[5:]
            m = _MENTION_ID.match(v) or _BARE_ID.match(v)
            if m:
                f.from_ids.add(int(m.group(1)))
            else:
                f.from_names.append(v.lstrip("@").lower())
        elif low.startswith("has:") and low[4:] in HAS_KINDS:
            f.has.add(low[4:])
        elif low.startswith("mentions:") and len(tok) > 9:
            v = tok[9:]
            m = _MENTION_ID.match(v) or _BARE_ID.match(v)
            if m:
                f.mention_ids.add(int(m.group(1)))
            else:
                f.mention_names.append(v.lstrip("@").lower())
        elif low.startswith("in:") and len(tok) > 3:
            v = tok[3:]
            m = re.match(r"<#(\d+)>$", v)
            if m:
                f.in_ids.add(int(m.group(1)))
            else:
                f.in_names.append(v.lstrip("#").lower())
        elif low.startswith("before:"):
            f.before = _parse_date(tok[7:]) or f.before
        elif low.startswith("after:"):
            f.after = _parse_date(tok[6:]) or f.after
        else:
            words.append(tok)
    f.text = " ".join(words)
    return f


def _msg_has(m, kind: str) -> bool:
    atts = getattr(m, "attachments", []) or []
    embeds = getattr(m, "embeds", []) or []
    content = getattr(m, "content", "") or ""

    def ct(a):
        return (getattr(a, "content_type", None) or "").lower()

    comps = getattr(m, "components", []) or []
    if kind == "image":
        return (any(ct(a).startswith("image") for a in atts)
                or any(getattr(getattr(e, "image", None), "url", None)
                       or getattr(getattr(e, "thumbnail", None), "url", None) for e in embeds)
                or any(_ctype(c) in ("thumbnail", "media_gallery") for c in walk_components(comps)))
    if kind == "video":
        return (any(ct(a).startswith("video") for a in atts)
                or any(getattr(e, "type", "") == "gifv" or getattr(getattr(e, "video", None), "url", None)
                       for e in embeds))
    if kind == "audio":
        return any(ct(a).startswith("audio") for a in atts)
    if kind == "file":
        return bool(atts) or any(_ctype(c) == "file" for c in walk_components(comps))
    if kind == "embed":
        return bool(embeds) or any(_ctype(c) == "container" for c in comps)
    if kind == "sticker":
        return bool(getattr(m, "stickers", []) or [])
    if kind in ("link", "url"):
        return bool(_URL_IN.search(content)) or any(getattr(e, "url", None) for e in embeds)
    if kind == "gif":
        low = content.lower()
        return (".gif" in low or "tenor.com" in low or "giphy.com" in low
                or any(getattr(e, "type", "") == "gifv" for e in embeds))
    return False


def author_matches(aid: int, name: str, f: SearchFilters) -> bool:
    if aid in f.from_ids:
        return True
    low = (name or "").lower()
    return any(fn in low for fn in f.from_names)


def _ctype(c) -> str | None:
    t = getattr(c, "type", None)
    return getattr(t, "name", None) or (t if isinstance(t, str) else None)


def walk_components(components):
    """Yield every component in a Components-V2 tree (sections, containers, rows, accessories)."""
    for c in components or []:
        yield c
        yield from walk_components(getattr(c, "children", None) or [])
        acc = getattr(c, "accessory", None)
        if acc is not None:
            yield acc


def message_matches(m, f: SearchFilters) -> bool:
    """Does a (real or fake) message satisfy every filter in `f`?"""
    if f.has_from:
        name = getattr(m.author, "display_name", None) or getattr(m.author, "name", "")
        if not author_matches(getattr(m.author, "id", 0), name, f):
            return False
    if f.mention_ids or f.mention_names:
        hit = False
        for u in getattr(m, "mentions", []) or []:
            nm = (getattr(u, "display_name", None) or getattr(u, "name", "") or "").lower()
            if u.id in f.mention_ids or any(mn in nm for mn in f.mention_names):
                hit = True
                break
        if not hit:
            return False
    for kind in f.has:
        if not _msg_has(m, kind):
            return False
    created = getattr(m, "created_at", None)
    ts = created.timestamp() if created else None
    if f.before is not None and (ts is None or ts >= f.before):
        return False
    if f.after is not None and (ts is None or ts < f.after):
        return False
    if f.text:
        hay = (getattr(m, "clean_content", None) or getattr(m, "content", "") or "").lower()
        if f.text.lower() not in hay:
            return False
    return True


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
.chips{display:flex;flex-wrap:wrap;gap:6px;}
.chip.perm{color:#dbdee1;border-color:var(--brand);}
.note.warn{border-left-color:var(--warn);color:#f3c780;}
.heat{display:flex;align-items:flex-end;gap:3px;height:90px;margin-top:6px;}
.hb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;gap:3px;}
.hb .hbf{width:100%;background:var(--brand);border-radius:3px 3px 0 0;min-height:3px;}
.hb span{font-size:9px;color:var(--muted);}
.altgrid{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}
.altu{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:2px 10px 2px 2px;font-size:13px;color:#c4c7d0;}
.altu:hover{color:#fff;text-decoration:none;border-color:var(--brand);}
.altu img{width:22px;height:22px;border-radius:50%;}
.altburst{margin:8px 0;padding:8px 10px;background:#17171d;border:1px solid var(--line);border-radius:8px;}
.ann-form{display:flex;flex-direction:column;gap:12px;margin:12px 0 20px;max-width:560px;}
.ann-form label{font-size:13px;color:var(--muted);font-weight:600;}
.ann-form select,.ann-form input,.ann-form textarea{width:100%;background:#1e1e26;border:1px solid var(--line);
border-radius:8px;color:var(--text);padding:9px 11px;font:inherit;font-size:15px;margin-top:4px;}
.ann-form textarea{resize:vertical;line-height:1.5;}
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
.dc{display:flex;height:calc(100vh - 56px);height:calc(100dvh - 56px);position:relative;}
.dc-burger{display:none;cursor:pointer;background:none;border:none;color:var(--text);font-size:22px;line-height:1;padding:0 6px 0 0;margin:0;}
.dc-backdrop{display:none;}
.dc-side{width:240px;flex:0 0 240px;background:#17171d;border-right:1px solid var(--line);overflow-y:auto;padding:8px 6px;}
.dc-side .srv{font-weight:700;padding:6px 8px;border-bottom:1px solid var(--line);margin-bottom:6px;display:block;color:var(--text);}
.dc-cat{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:12px 8px 2px;font-weight:700;}
.dc-ch{display:block;padding:5px 8px;border-radius:6px;color:#9aa0b3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px;}
.dc-ch:hover{background:#22222c;color:var(--text);text-decoration:none;}
.dc-ch.on{background:#2d2d3a;color:#fff;}
.dc-ch.voice{color:#5c5f70;cursor:default;}
.dc-ch.voice:hover{background:none;color:#5c5f70;}
.dc-ch.vc{white-space:normal;overflow:visible;padding:5px 8px 2px;}
.vc-head{color:#9aa0b3;display:flex;align-items:center;gap:4px;}
.vc-count{margin-left:auto;font-size:11px;background:#2d2d3a;color:#b9bbbe;border-radius:8px;padding:0 6px;}
.vc-members{margin:2px 0 4px 14px;display:flex;flex-direction:column;gap:1px;}
.vc-m{display:flex;align-items:center;gap:6px;padding:3px 6px;border-radius:6px;color:#c4c7d0;font-size:13px;}
.vc-m:hover{background:#22222c;color:#fff;text-decoration:none;}
.vc-m.dim{color:#72757e;}
.vc-av{width:20px;height:20px;border-radius:50%;flex:none;}
.vc-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.vb{flex:none;margin-left:auto;font-size:11px;line-height:1;}
.vb+.vb{margin-left:4px;}
.vb.mut,.vb.deaf{filter:grayscale(1);opacity:.85;}
.vb.live{background:#EF4444;color:#fff;font-weight:700;border-radius:4px;padding:1px 4px;font-size:9px;letter-spacing:.03em;}
.vb.cam{opacity:.9;}
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
.m .reply{font-size:13px;color:var(--muted);margin-bottom:2px;display:flex;gap:4px;align-items:center;overflow:hidden;white-space:nowrap;}
.m .reply[data-jump]{cursor:pointer;}
.m .reply[data-jump]:hover .rt{color:#dbdee1;}
.m .reply[data-jump]:hover .rn{filter:brightness(1.2);}
.m .reply .ra{color:#4e5058;margin-right:2px;}
.m .reply .rav{width:16px;height:16px;border-radius:50%;flex:none;}
.m .reply .rn{color:#b9bbbe;font-weight:600;flex:none;}
.m .reply .rt{overflow:hidden;text-overflow:ellipsis;min-width:0;}
.m .reply .rt .ie{width:16px;height:16px;vertical-align:-3px;}
.m .reply .rmeta{font-style:italic;opacity:.8;}
.m.flash{animation:mflash 1.4s ease-out;border-radius:4px;}
@keyframes mflash{0%,30%{background:rgba(88,101,242,.28);}100%{background:transparent;}}
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
.emb{border-left:4px solid var(--brand);background:#2b2d31;border-radius:4px;padding:8px 12px;margin-top:4px;max-width:520px;display:flex;gap:10px;}
.emb .emb-main{min-width:0;flex:1;}
.emb .et{font-weight:600;margin:2px 0;display:block;color:#8ab4f8;}
.emb .ed{color:#dbdee1;font-size:14px;white-space:pre-wrap;word-break:break-word;}
.emb .emb-img{display:block;max-width:100%;max-height:300px;border-radius:6px;margin-top:8px;}
.emb .ea{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:#dbdee1;margin-bottom:2px;}
.emb .ea-ic{width:20px;height:20px;border-radius:50%;}
.emb .efs{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:6px;}
.emb .ef{min-width:100%;}
.emb .ef.inline{min-width:30%;flex:1;}
.emb .efn{font-size:13px;font-weight:600;color:#fff;}
.emb .efv{font-size:13px;color:#c4c7d0;white-space:pre-wrap;word-break:break-word;}
.emb .eft{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);margin-top:8px;}
.emb .ef-ic{width:16px;height:16px;border-radius:50%;}
.emb .eth{width:72px;height:72px;border-radius:6px;object-fit:cover;flex:none;}
.cv-box{border-left:4px solid var(--brand);background:#2b2d31;border-radius:6px;padding:8px 12px;margin-top:4px;max-width:520px;}
.cv-text{color:#dbdee1;font-size:14px;white-space:pre-wrap;word-break:break-word;margin:2px 0;}
.cv-section{display:flex;gap:10px;align-items:flex-start;}
.cv-section .cv-main{flex:1;min-width:0;}
.cv-thumb img{width:80px;height:80px;border-radius:6px;object-fit:cover;}
.cv-gallery{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;}
.cv-gallery img{max-width:160px;max-height:160px;border-radius:6px;}
.cv-sep{border:none;border-top:1px solid var(--line);margin:6px 0;}
.cv-btn{display:inline-block;margin:4px 4px 0 0;font-size:12px;background:#3a3c43;border:1px solid var(--line);border-radius:6px;padding:3px 10px;color:#dbdee1;}
.cv-btn:hover{border-color:var(--brand);text-decoration:none;}
.sr-ch{font-size:12px;color:var(--muted);margin-left:8px;}
.fwd{border-left:3px solid #4e5058;background:#2b2d3155;border-radius:4px;padding:4px 10px;margin-top:4px;max-width:520px;}
.fwd-tag{font-size:11px;color:var(--muted);font-style:italic;margin-bottom:2px;}
.rx{display:inline-flex;gap:5px;align-items:center;background:#2b2d31;border:1px solid var(--line);border-radius:8px;padding:1px 7px;font-size:12px;margin:5px 4px 0 0;color:#b9bbbe;}
button.rx.rxc{cursor:pointer;font-family:inherit;}
button.rx.rxc:hover{border-color:var(--brand);color:#fff;}
.rx-panel{margin:6px 0 2px;background:#17171d;border:1px solid var(--line);border-radius:8px;padding:8px 10px;max-width:460px;}
.rxp-empty{color:var(--muted);font-size:13px;}
.rxp-group{display:flex;gap:8px;align-items:flex-start;padding:4px 0;border-top:1px solid var(--line);}
.rxp-group:first-child{border-top:none;}
.rxp-emoji{display:flex;align-items:center;gap:3px;flex:none;color:#b9bbbe;font-size:13px;min-width:38px;}
.rxp-emoji .ie{width:18px;height:18px;}
.rxp-users{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;}
.rxp-u{display:inline-flex;align-items:center;gap:4px;color:#c4c7d0;font-size:12px;background:#2b2d31;border-radius:10px;padding:1px 8px 1px 2px;}
.rxp-u:hover{color:#fff;text-decoration:none;background:#363a44;}
.rxp-u img{width:18px;height:18px;border-radius:50%;}
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
/* Phone layout: channel list becomes a slide-in drawer, media fits the screen. */
@media(max-width:820px){
  .dc-burger{display:inline-flex;align-items:center;}
  .dc-side{position:absolute;top:0;bottom:0;left:0;z-index:30;width:80%;max-width:300px;flex-basis:80%;
    transform:translateX(-100%);transition:transform .22s ease;box-shadow:6px 0 24px #000a;}
  .dc.show-side .dc-side{transform:translateX(0);}
  .dc.show-side .dc-backdrop{display:block;position:absolute;inset:0;z-index:20;background:#0008;}
  .dc-head{padding:10px 12px;flex-wrap:wrap;gap:8px;position:sticky;top:0;z-index:10;}
  .dc-head .topic{display:none;}
  .ch-search{order:3;flex:1 1 100%;}
  .ch-search input{width:100%;font-size:16px;}
  .dc-msgs{padding:12px 10px 18px;}
  .m{gap:10px;padding:2px 2px;}
  .m .av,.m .av img{width:36px;flex-basis:36px;height:36px;}
  .att img,.att-media{max-width:100%;}
  .att-audio audio{width:100%;}
  .emb,.cv-box,.fwd,.rx-panel,.att-file{max-width:100%;}
  .cv-gallery img{max-width:46%;}
  .dc-input textarea{font-size:16px;}
  .prof{padding:18px 14px;}
  .prof-grid{grid-template-columns:repeat(auto-fit,minmax(130px,1fr));}
  .heat{height:70px;}
  .wrap{padding:14px;}
  header{padding:10px 14px;gap:10px;}
}
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
.ie{width:1.375em;height:1.375em;object-fit:contain;vertical-align:-0.3em;}
.jumbo .ie{width:2.6em;height:2.6em;}
.mention{background:#5865f233;color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500;}
.sticker{max-width:160px;max-height:160px;border-radius:8px;margin-top:4px;display:block;}
.rx .ie{width:1.1em;height:1.1em;vertical-align:-0.2em;}
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
        # channel_id -> set of live WebSocket connections watching that channel
        self._subs: dict[int, set] = defaultdict(set)
        self._add_routes()
        bot.add_listener(self._on_new_message, "on_message")
        bot.add_listener(self._on_edit_message, "on_message_edit")
        bot.add_listener(self._on_delete_message, "on_message_delete")

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
        a("/g/{gid}/alts", self.alt_scan)
        a("/g/{gid}/announce", self.announce_page)
        p("/g/{gid}/announce", self.announce_send)
        a("/g/{gid}/c/{cid}", self.channel)
        a("/g/{gid}/c/{cid}/messages", self.channel_messages)
        a("/g/{gid}/c/{cid}/m/{mid}/reactions", self.message_reactions)
        a("/g/{gid}/c/{cid}/media", self.channel_media)
        a("/g/{gid}/c/{cid}/ws", self.channel_ws)
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
            f"<a href='/g/{guild.id}/emojis'>{emoji_count} emojis</a> · "
            f"<a href='/g/{guild.id}/alts'>🔎 Alt &amp; raid scan</a> · "
            f"<a href='/g/{guild.id}/announce'>📣 Announce features</a></div>"
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
                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    rows.append(self._voice_block(guild, ch))
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

    def _voice_block(self, guild: discord.Guild, ch) -> str:
        """A voice/stage channel plus whoever's connected, with their mute/deafen state."""
        members = list(getattr(ch, "members", []) or [])
        icon = "🎙️" if isinstance(ch, discord.StageChannel) else "🔊"
        count = f"<span class='vc-count'>{len(members)}</span>" if members else ""
        head = f"<div class='vc-head'>{icon} {esc(ch.name)}{count}</div>"
        rows = []
        for m in members:
            vs = getattr(m, "voice", None)
            badges = []
            if vs is not None:
                deaf = getattr(vs, "deaf", False) or getattr(vs, "self_deaf", False)
                mute = getattr(vs, "mute", False) or getattr(vs, "self_mute", False)
                srv = " (server)" if getattr(vs, "deaf", False) or getattr(vs, "mute", False) else ""
                if deaf:
                    badges.append(f"<span class='vb deaf' title='Deafened{srv}'>🔇</span>")
                elif mute:
                    badges.append(f"<span class='vb mut' title='Muted{srv}'>🎙️</span>")
                if getattr(vs, "self_stream", False):
                    badges.append("<span class='vb live' title='Streaming'>LIVE</span>")
                if getattr(vs, "self_video", False):
                    badges.append("<span class='vb cam' title='Camera on'>📹</span>")
            av = getattr(getattr(m, "display_avatar", None), "url", "")
            name = esc(getattr(m, "display_name", "member"))
            dim = " dim" if (vs is not None and (getattr(vs, "deaf", False) or getattr(vs, "self_deaf", False)
                                                  or getattr(vs, "mute", False) or getattr(vs, "self_mute", False))) else ""
            rows.append(
                f"<a class='vc-m{dim}' href='/g/{guild.id}/u/{m.id}'>"
                f"<img class='vc-av' src='{esc(av)}' alt='' loading='lazy'>"
                f"<span class='vc-name'>{name}</span>{''.join(badges)}</a>")
        body = f"<div class='vc-members'>{''.join(rows)}</div>" if rows else ""
        return f"<div class='dc-ch voice vc'>{head}{body}</div>"

    def _channel_header(self, guild: discord.Guild, channel, active: str, query: str = "") -> str:
        topic = getattr(channel, "topic", None)
        topic_html = f"<span class='topic'>{esc(topic[:120])}</span>" if topic else ""
        base = f"/g/{guild.id}/c/{channel.id}"
        search = (f"<form class='ch-search' method='get' action='{base}'>"
                  f"<input name='q' value='{esc(query)}' autocomplete='off' "
                  f"placeholder='Search — try from:@user has:image in:#channel after:2025-01-01' "
                  f"title='Operators: from:@user or from:name · mentions:@user · in:#channel · "
                  f"has:image|video|audio|file|link|embed|sticker|gif · before:YYYY-MM-DD · after:YYYY-MM-DD'></form>")
        tabs = (f"<div class='ch-tabs'>"
                f"<a class='{'on' if active == 'chat' else ''}' href='{base}'>Chat</a>"
                f"<a class='{'on' if active == 'media' else ''}' href='{base}/media'>Media</a></div>")
        burger = ("<button class='dc-burger' type='button' aria-label='Channels' "
                  "onclick=\"this.closest('.dc').classList.toggle('show-side')\">☰</button>")
        return (f"<div class='dc-head'>{burger}<span class='hash'>#</span> "
                f"{esc(channel.name)}{topic_html}{search}{tabs}</div>")

    def _highlight(self, text: str, query: str) -> str:
        safe = esc(text)
        if not query:
            return safe
        try:
            pattern = re.compile(re.escape(esc(query)), re.IGNORECASE)
        except re.error:
            return safe
        return pattern.sub(lambda mo: f"<mark>{mo.group(0)}</mark>", safe)

    @staticmethod
    def _result_text(m) -> str:
        """A one-line description of a message for search results, even if it's media-only."""
        txt = (getattr(m, "clean_content", None) or getattr(m, "content", "") or "").strip()
        if txt:
            return txt
        atts = getattr(m, "attachments", []) or []
        if atts:
            return f"📎 {getattr(atts[0], 'filename', 'attachment')}" + (f" +{len(atts) - 1}" if len(atts) > 1 else "")
        if getattr(m, "stickers", []):
            return "[sticker]"
        for e in getattr(m, "embeds", []) or []:
            return f"[embed] {esc(getattr(e, 'title', '') or '')}".strip()
        for c in walk_components(getattr(m, "components", []) or []):
            if _ctype(c) == "text_display":
                t = (getattr(c, "content", "") or "").strip()
                if t:
                    return t
        if getattr(m, "components", []):
            return "[card]"
        return "(no text)"

    def _search_targets(self, guild: discord.Guild, channel, f: SearchFilters) -> list:
        """Which channels to search: the current one, or those named by in:#channel."""
        if not f.has_in:
            return [channel]
        hits = [c for c in guild.text_channels
                if c.id in f.in_ids or any(n in c.name.lower() for n in f.in_names)]
        return hits or [channel]

    async def _search_channel(self, guild: discord.Guild, channel, f: SearchFilters) -> tuple[list, bool]:
        """Search one channel; returns (rows, scanned_live). Each row: (mid, name, content, created, channel)."""
        can = channel.permissions_for(guild.me).read_message_history
        live = can and (f.structural or (f.has_from and not f.text))
        rows = []
        if live:
            with contextlib.suppress(discord.HTTPException):
                async for m in channel.history(limit=SEARCH_SCAN):
                    if message_matches(m, f):
                        rows.append((m.id, m.author.display_name, self._result_text(m),
                                     m.created_at.timestamp(), channel))
                        if len(rows) >= 80:
                            break
            return rows, True
        stored = await asyncio.to_thread(self.bot.storage.search_channel, guild.id, channel.id,
                                         f.text, 400 if f.has_from else 80)
        for mid, aid, aname, content, created in stored:
            if f.has_from and not author_matches(aid, aname, f):
                continue
            rows.append((mid, aname or f"user {aid}", content, created, channel))
            if len(rows) >= 80:
                break
        if not rows and can:
            with contextlib.suppress(discord.HTTPException):
                async for m in channel.history(limit=MEDIA_SCAN):
                    if message_matches(m, f):
                        rows.append((m.id, m.author.display_name, self._result_text(m),
                                     m.created_at.timestamp(), channel))
                        if len(rows) >= 80:
                            break
            return rows, True
        return rows, False

    async def _search_html(self, guild: discord.Guild, channel, query: str) -> str:
        f = parse_search(query)
        targets = self._search_targets(guild, channel, f)
        results, scanned = [], False
        for ch in targets[:8]:
            rows, live = await self._search_channel(guild, ch, f)
            results.extend(rows)
            scanned = scanned or live
        results.sort(key=lambda r: r[3] or 0, reverse=True)
        results = results[:80]
        multi = len(targets) > 1
        clear = f"<a href='/g/{guild.id}/c/{channel.id}'>Clear</a>"
        scope = f" across {len(targets)} channels" if multi else ""
        head = (f"<div class='sr-head'><span>{len(results)} result(s) for "
                f"<strong>{esc(query)}</strong>{scope}</span>{clear}</div>")
        note = "<div class='note'>Scanning recent history for matches.</div>" if scanned else ""
        if not results:
            return head + "<div class='dc-empty'>No messages matched.</div>"
        items = []
        for mid, name, content, created, ch in results:
            ts = f"<time class='ts' data-ts='{int(created)}'></time>" if created else ""
            where = f"<a class='sr-ch' href='/g/{guild.id}/c/{ch.id}'>#{esc(ch.name)}</a>" if multi else ""
            jump = (f"<a class='jump' href='/g/{guild.id}/c/{ch.id}?to={mid}'>Jump →</a>") if mid else ""
            items.append(f"<div class='sr'><div class='head'><span class='name'>{esc(name)}</span>"
                         f"{where}{ts}{jump}</div>"
                         f"<div class='text'>{self._highlight(content, f.text)}</div></div>")
        return head + note + "".join(items)

    @staticmethod
    def _is_message(x) -> bool:
        # Duck-typed: a resolved reference is a full message, not a bare stub.
        return hasattr(x, "author") and hasattr(x, "id") and hasattr(x, "content")

    @staticmethod
    def _name_colour(member) -> str:
        colour = getattr(getattr(member, "colour", None), "value", 0)
        return f" style='color:#{colour:06x}'" if colour else ""

    @staticmethod
    def _attachment_html(a) -> str:
        ct = (getattr(a, "content_type", None) or "").lower()
        url = esc(a.url)
        fn = esc(getattr(a, "filename", "file"))
        if ct.startswith("image"):
            return (f"<a class='att' href='{url}' target='_blank' rel='noreferrer'>"
                    f"<img src='{url}' alt='{fn}' loading='lazy'></a>")
        if ct.startswith("video"):
            return (f"<div class='att'><video class='att-media' src='{url}' controls "
                    f"preload='metadata'></video></div>")
        if ct.startswith("audio"):
            return (f"<div class='att att-audio'><div class='fn'>🎵 {fn}</div>"
                    f"<audio src='{url}' controls preload='none'></audio></div>")
        kb = f"{a.size // 1024} KB" if getattr(a, "size", 0) else "file"
        return (f"<a class='att att-file' href='{url}' target='_blank' rel='noreferrer'>"
                f"<span class='fi'>📎</span><span class='fn'>{fn}<br><span class='muted'>{kb}</span></span></a>")

    def _forward_html(self, m, guild) -> str:
        """Render forwarded message snapshots (Discord 'Forward') as a quoted block."""
        snaps = getattr(m, "message_snapshots", None) or []
        blocks = []
        for s in snaps:
            names = _mention_names(s)
            inner = []
            content = getattr(s, "content", "") or ""
            if content.strip():
                inner.append(f"<div class='text'>{render_content(content, guild, names)}</div>")
            for st in getattr(s, "stickers", []) or []:
                url = getattr(st, "url", None)
                if url:
                    inner.append(f"<img class='sticker' src='{esc(url)}' alt='' loading='lazy'>")
            inner.extend(self._attachment_html(a) for a in getattr(s, "attachments", []) or [])
            inner.extend(self._embed_html(e, guild, names) for e in getattr(s, "embeds", []) or [])
            inner.append(self._components_html(getattr(s, "components", []) or [], guild, names))
            body = "".join(x for x in inner if x) or "<div class='text empty'>(no content)</div>"
            blocks.append(f"<div class='fwd'><div class='fwd-tag'>↪ Forwarded</div>{body}</div>")
        return "".join(blocks)

    def _one_message(self, m: discord.Message, prev: discord.Message | None,
                     refs: dict | None = None, deleted: set | None = None) -> str:
        author = m.author
        same = (prev is not None and prev.author.id == author.id
                and (m.created_at - prev.created_at).total_seconds() < 420
                and m.reference is None and prev.reference is None)
        avatar = getattr(getattr(author, "display_avatar", None), "url", "")
        colour = getattr(getattr(author, "colour", None), "value", 0)
        name_style = f" style='color:#{colour:06x}'" if colour else ""
        app = "<span class='app'>APP</span>" if getattr(author, "bot", False) else ""
        ts = int(m.created_at.timestamp())
        guild = getattr(m, "guild", None)
        reply = ""
        reftype = getattr(getattr(m.reference, "type", None), "name", "default") if m.reference else None
        # A forward's reference points at the source, not a reply — don't render a reply stub for it.
        if m.reference is not None and reftype != "forward":
            ref = m.reference.resolved
            if not self._is_message(ref) and refs:
                ref = refs.get(getattr(m.reference, "message_id", None)) or ref
            ref_mid = getattr(m.reference, "message_id", None)
            if self._is_message(ref):
                ref_av = getattr(getattr(ref.author, "display_avatar", None), "url", "")
                ref_name = getattr(ref.author, "display_name", "someone")
                snippet_txt = " ".join((ref.content or "").split())
                if snippet_txt:
                    snippet = render_content(snippet_txt[:120], guild, _mention_names(ref))
                elif getattr(ref, "attachments", None):
                    snippet = "<span class='rmeta'>📎 attachment</span>"
                elif getattr(ref, "embeds", None) or getattr(ref, "stickers", None):
                    snippet = "<span class='rmeta'>embed</span>"
                else:
                    snippet = "<span class='rmeta'>(no text)</span>"
                jump = (f"https://discord.com/channels/{getattr(guild, 'id', 0)}/"
                        f"{getattr(m, 'channel', None) and m.channel.id}/{ref.id}")
                reply = (f"<div class='reply' data-jump='{ref.id}' data-href='{esc(jump)}'>"
                         f"<span class='ra'>↳</span><img class='rav' src='{esc(ref_av)}' alt=''>"
                         f"<span class='rn'{self._name_colour(ref.author)}>{esc(ref_name)}</span>"
                         f"<span class='rt'>{snippet}</span></div>")
            elif ref_mid and deleted and ref_mid in deleted:
                reply = ("<div class='reply'><span class='ra'>↳</span>"
                         "<span class='rt rmeta'>Original message was deleted</span></div>")
            else:
                # Unresolved but not known-deleted — a clickable stub, not a false "deleted".
                jump = (f"https://discord.com/channels/{getattr(guild, 'id', 0)}/"
                        f"{getattr(m, 'channel', None) and m.channel.id}/{ref_mid}")
                data = f" data-jump='{ref_mid}' data-href='{esc(jump)}'" if ref_mid else ""
                reply = (f"<div class='reply'{data}><span class='ra'>↳</span>"
                         "<span class='rt rmeta'>replying to a message</span></div>")
        raw = m.content or ""
        names = _mention_names(m)
        text_html = render_content(raw, guild, names)
        # Discord shows a message that's only custom emoji at jumbo size.
        only_emoji = bool(raw.strip()) and not _TOKEN.sub("", raw).strip() and bool(_EMOJI.search(raw))
        text = f"<div class='text{' jumbo' if only_emoji else ''}'>{text_html}</div>" if text_html else ""
        stickers = []
        for s in getattr(m, "stickers", []) or []:
            url = getattr(s, "url", None)
            if url:
                stickers.append(f"<img class='sticker' src='{esc(url)}' "
                                f"alt='{esc(getattr(s, 'name', 'sticker'))}' loading='lazy'>")
        atts = [self._attachment_html(a) for a in m.attachments]
        embeds = [self._embed_html(e, guild, names) for e in m.embeds]
        embeds = [h for h in embeds if h]
        components_html = self._components_html(getattr(m, "components", []) or [], guild, names)
        forward_html = self._forward_html(m, guild)
        rx = []
        for r in getattr(m, "reactions", []):
            e = r.emoji
            if getattr(e, "id", None):
                glyph = f"<img class='ie' src='{esc(getattr(e, 'url', ''))}' alt='{esc(getattr(e, 'name', ''))}'>"
            else:
                glyph = esc(str(e))
            rx.append(f"<button type='button' class='rx rxc' title='See who reacted'>{glyph} {r.count}</button>")
        reactions = f"<div class='rxs'>{''.join(rx)}</div>" if rx else ""
        body = text
        if (not text and not stickers and not m.attachments and not m.embeds
                and not components_html and not forward_html):
            body = "<div class='text empty'>(no text content)</div>"
        profile = f"/g/{getattr(guild, 'id', 0)}/u/{author.id}"
        head = ("" if same else
                f"<div class='head'><a class='name' href='{profile}'{name_style}>{esc(author.display_name)}{app}</a>"
                f"<time class='ts' data-ts='{ts}'></time></div>")
        av = f"<a class='av' href='{profile}'><img src='{esc(avatar)}' alt='' loading='lazy'></a>"
        return (f"<div class='m{' grouped' if same else ''}' data-mid='{m.id}'>{av}"
                f"<div class='content'>{reply}{head}{body}{''.join(stickers)}"
                f"{''.join(atts)}{''.join(embeds)}{components_html}{forward_html}{reactions}</div></div>")

    async def _messages_html(self, channel, before: int | None = None, around: int | None = None) -> str:
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
        if around is not None:
            kwargs["around"] = discord.Object(id=around)   # Discord-style "jump to" window
        elif before is not None:
            kwargs["before"] = discord.Object(id=before)
        try:
            collected = [m async for m in channel.history(**kwargs)]
        except discord.HTTPException:
            return "" if before is not None else "<div class='note err'>Couldn't read live history just now.</div>"
        collected.reverse()
        if not collected:
            return "" if before is not None else "<div class='dc-empty'>No messages yet.</div>"
        refs, deleted = await self._resolve_refs(channel, collected)
        html = []
        prev = None
        for m in collected:
            html.append(self._one_message(m, prev, refs, deleted))
            prev = m
        return "".join(html)

    async def _resolve_refs(self, channel, messages: list) -> dict:
        """Map referenced-message id -> Message for replies on this page.

        Prefers messages already in this batch and the payload's resolved
        reference; fetches anything still missing, capped so a page of replies
        can't spray the API."""
        refs: dict[int, discord.Message] = {}
        deleted: set[int] = set()
        by_id = {m.id: m for m in messages}
        need: list[int] = []
        for m in messages:
            ref = getattr(m, "reference", None)
            # Forwards reference their source, not a reply — never resolve them here.
            if ref is None or getattr(getattr(ref, "type", None), "name", "default") == "forward":
                continue
            mid = getattr(ref, "message_id", None)
            if mid is None:
                continue
            resolved = getattr(ref, "resolved", None)
            if self._is_message(resolved):
                refs[mid] = resolved
            elif mid in by_id:
                refs[mid] = by_id[mid]
            else:
                need.append(mid)
        budget = 15
        for mid in need:
            if budget <= 0:
                break
            budget -= 1
            try:
                refs[mid] = await channel.fetch_message(mid)
            except discord.NotFound:
                deleted.add(mid)  # confirmed gone
            except discord.HTTPException:
                pass  # couldn't load (no access / budget) — show a neutral stub, not "deleted"
        return refs, deleted

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
        to_raw = request.query.get("to")
        jump_to = int(to_raw) if to_raw and to_raw.isdigit() else None
        if searching:
            messages = await self._search_html(guild, channel, query)
        else:
            messages = await self._messages_html(channel, around=jump_to)
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
            f"<div class='dc'>{sidebar}"
            "<div class='dc-backdrop' onclick=\"this.closest('.dc').classList.remove('show-side')\"></div>"
            f"<div class='dc-main'>{head}"
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
            f"var JUMP={jump_to if jump_to else 'null'};"
            "function bot(){return pane.scrollHeight-pane.scrollTop-pane.clientHeight<100;}"
            "stamp(document);"
            # Click a reply preview to jump to the message it answers.
            "function jumpTo(id){var t=pane.querySelector(\".m[data-mid='\"+id+\"']\");"
            "if(!t)return false;t.scrollIntoView({block:'center'});"
            "t.classList.remove('flash');void t.offsetWidth;t.classList.add('flash');return true;}"
            "pane.addEventListener('click',function(e){var r=e.target.closest('.reply[data-jump]');if(!r)return;"
            "var id=r.dataset.jump;if(jumpTo(id))return;"
            # Not on this page — reopen the channel centred on that message, in-dashboard.
            "location.href=location.pathname+'?to='+id;});"
            # Click a reaction to see who reacted (toggles a panel under the message).
            "pane.addEventListener('click',async function(e){var b=e.target.closest('.rx');if(!b)return;"
            "var m=b.closest('.m'),wrap=b.closest('.rxs');if(!m||!wrap)return;"
            "var ex=wrap.querySelector('.rx-panel');if(ex){ex.remove();return;}"
            "var d=document.createElement('div');d.className='rx-panel';d.textContent='…';wrap.appendChild(d);"
            "try{var r=await fetch(location.pathname+'/m/'+m.dataset.mid+'/reactions');"
            "d.innerHTML=r.ok?await r.text():'<div class=rxp-empty>Could not load.</div>';}"
            "catch(err){d.innerHTML='<div class=rxp-empty>Could not load.</div>';}});"
            + ("" if searching else
               "if(JUMP){setTimeout(function(){if(!jumpTo(JUMP))pane.scrollTop=pane.scrollHeight;},60);}"
               "else{pane.scrollTop=pane.scrollHeight;}"
               # Live updates over a WebSocket — instant, no flicker, like a real client.
               "function nodeOf(h){var t=document.createElement('div');t.innerHTML=h;return t.firstElementChild;}"
               "function append(h){var b=bot(),n=nodeOf(h);if(!n)return;"
               "if(pane.querySelector(\".m[data-mid='\"+n.dataset.mid+\"']\"))return;"
               "pane.appendChild(n);stamp(n);if(b)pane.scrollTop=pane.scrollHeight;}"
               "function editM(id,h){var e=pane.querySelector(\".m[data-mid='\"+id+\"']\"),n=nodeOf(h);"
               "if(e&&n){e.replaceWith(n);stamp(n);}}"
               "function delM(id){var e=pane.querySelector(\".m[data-mid='\"+id+\"']\");if(e)e.remove();}"
               "var ws,pt;"
               "function poll(){if(pt)return;pt=setInterval(async function(){if(!bot())return;try{"
               "var r=await fetch(location.pathname+'/messages');if(!r.ok)return;var h=await r.text();"
               "pane.innerHTML=h;stamp(pane);pane.scrollTop=pane.scrollHeight;}catch(e){}},5000);}"
               "function nopoll(){if(pt){clearInterval(pt);pt=null;}}"
               "function connect(){try{ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')"
               "+location.host+location.pathname+'/ws');}catch(e){poll();return;}"
               "ws.onopen=function(){nopoll();};"
               "ws.onmessage=function(ev){var d;try{d=JSON.parse(ev.data);}catch(e){return;}"
               "if(d.t==='new')append(d.html);else if(d.t==='edit')editM(d.mid,d.html);else if(d.t==='del')delM(d.mid);};"
               "ws.onclose=function(){poll();setTimeout(connect,4000);};"
               "ws.onerror=function(){try{ws.close();}catch(e){}};}"
               "connect();"
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

    async def alt_scan(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        return layout(f"Alt scan · {guild.name}", self._alts_body(guild), user=user)

    async def announce_page(self, request: web.Request) -> web.Response:
        user = self._user(request)
        if user is None:
            raise web.HTTPFound("/login")
        guild = self._guild_or_404(request)
        return layout(f"Announce · {guild.name}", self._announce_body(guild, user[0], request), user=user)

    def _announce_body(self, guild: discord.Guild, user_id: int, request: web.Request) -> str:
        csrf = self._hidden_csrf(user_id)
        opts = []
        for ch in getattr(guild, "text_channels", []) or []:
            if ch.permissions_for(guild.me).send_messages:
                opts.append(f"<option value='{ch.id}'>#{esc(ch.name)}</option>")
        channels = "".join(opts) or "<option disabled>No channels SYKE can post in</option>"
        return (
            f"<div class='prof'><div class='muted'><a href='/g/{guild.id}'>← {esc(guild.name)}</a></div>"
            "<h1>📣 Announce new features</h1>"
            "<p class='muted'>Posts a compact embed as SYKE. Edit the copy below; the preview updates live.</p>"
            + flash(request) +
            f"<form method='post' action='/g/{guild.id}/announce' class='ann-form'>{csrf}"
            f"<label>Channel<br><select name='cid' required>{channels}</select></label>"
            f"<label>Title<br><input name='title' id='an-title' maxlength='256' value='{esc(ANNOUNCE_TITLE)}'></label>"
            f"<label>Message<br><textarea name='body' id='an-body' rows='12' maxlength='4000'>{esc(ANNOUNCE_BODY)}</textarea></label>"
            "<button class='btn brand'>Post announcement</button></form>"
            "<h3>Preview</h3>"
            "<div class='emb' style='border-left-color:#8B5CF6'><div class='emb-main'>"
            "<div class='et' id='pv-title'></div><div class='ed' id='pv-body'></div>"
            "<div class='eft'><span>SYKE • !help</span></div></div></div></div>"
            "<script>var T=document.getElementById('an-title'),B=document.getElementById('an-body'),"
            "pt=document.getElementById('pv-title'),pb=document.getElementById('pv-body');"
            "function esc(s){return s.replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}"
            "function up(){pt.textContent=T.value;"
            "pb.innerHTML=esc(B.value).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')"
            ".replace(/`(.+?)`/g,'<code>$1</code>').replace(/\\n/g,'<br>');}"
            "T.addEventListener('input',up);B.addEventListener('input',up);up();</script>"
        )

    async def announce_send(self, request: web.Request) -> web.Response:
        guild, data, _ = await self._post_guild(request)
        back = f"/g/{guild.id}/announce"
        try:
            cid = int((data.get("cid") or "").strip())
        except ValueError:
            raise self._redirect(back, err="Pick a channel.")
        channel = guild.get_channel(cid)
        title = (data.get("title") or "").strip()[:256] or ANNOUNCE_TITLE
        body = (data.get("body") or "").strip()[:4000]
        if channel is None or not hasattr(channel, "send") or not body:
            raise self._redirect(back, err="Nothing to announce, or that channel can't be used.")
        em = discord.Embed(title=title, description=body, colour=discord.Colour.from_str(BRAND),
                           timestamp=datetime.now(timezone.utc))
        em.set_footer(text="SYKE • !help")
        try:
            await channel.send(embed=em, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        except discord.HTTPException:
            raise self._redirect(back, err="Discord refused to post that.")
        log.info("Owner posted a feature announcement in %s/#%s", guild.id, getattr(channel, "name", cid))
        raise self._redirect(back, ok="Announcement posted.")

    def _alts_body(self, guild: discord.Guild) -> str:
        now = datetime.now(timezone.utc)
        members = [m for m in getattr(guild, "members", []) or [] if not getattr(m, "bot", False)]

        def card(uid, extra=""):
            m = guild.get_member(uid)
            name = getattr(m, "display_name", None) or f"user {uid}"
            av = getattr(getattr(m, "display_avatar", None), "url", "")
            return (f"<a class='altu' href='/g/{guild.id}/u/{uid}'>"
                    f"<img src='{esc(av)}' alt='' loading='lazy'><span>{esc(name)}</span>"
                    f"{(' <span class=muted>' + extra + '</span>') if extra else ''}</a>")

        secs = [f"<div class='muted'><a href='/g/{guild.id}'>← {esc(guild.name)}</a></div>"
                "<h1>🔎 Alt &amp; raid scan</h1>"
                "<p class='muted'>Heuristics over this server's own member and join data — signals, not proof.</p>"]

        # Youngest accounts (and whether they've set an avatar).
        aged = sorted((m for m in members if getattr(m, "created_at", None)),
                      key=lambda m: m.created_at, reverse=True)[:20]
        young = [m for m in aged if (now - m.created_at).days < 30]
        if young:
            rows = "".join(card(m.id, f"{rough_age(m.created_at, now)} old"
                                 + ("  · no avatar" if getattr(m, "avatar", True) is None else ""))
                           for m in young)
            secs.append(f"<div class='prof-sec'><h3>Newest accounts ({len(young)} under 30 days)</h3>"
                        f"<div class='altgrid'>{rows}</div></div>")

        # Join bursts — many members arriving within a minute of each other.
        joins = self.bot.storage.joins_all(guild.id)
        clusters = join_clusters(joins, window=60, min_size=3)
        if clusters:
            blocks = []
            for cl in clusters[:10]:
                blocks.append(f"<div class='altburst'><div class='muted'>{len(cl)} joined together</div>"
                              f"<div class='altgrid'>{''.join(card(uid) for uid in cl)}</div></div>")
            secs.append(f"<div class='prof-sec'><h3>Join bursts ({len(clusters)})</h3>{''.join(blocks)}</div>")

        # Who's inviting the most members.
        by_inviter: dict[int, list[int]] = defaultdict(list)
        names: dict[int, str] = {}
        for uid, inviter_id, inviter_name, code, kind, _ in joins:
            if inviter_id:
                by_inviter[inviter_id].append(uid)
                names[inviter_id] = inviter_name or f"user {inviter_id}"
        ranked = sorted(by_inviter.items(), key=lambda kv: len(kv[1]), reverse=True)[:12]
        if ranked:
            items = "".join(
                f"<li><a href='/g/{guild.id}/u/{iid}'>{esc(names[iid])}</a> — {len(uids)} member(s)</li>"
                for iid, uids in ranked)
            secs.append(f"<div class='prof-sec'><h3>Top inviters</h3><ul>{items}</ul></div>")

        if len(secs) == 2:  # only the header + intro
            secs.append("<div class='note'>Nothing flagged — no very new accounts, join bursts or invite data yet.</div>")
        return "<div class='prof'>" + "".join(secs) + "</div>"

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
        now_dt = datetime.now(timezone.utc)

        def when(dt):
            return f"<time data-ts='{int(dt.timestamp())}'></time>" if dt else "—"

        stats = [
            ("Stored messages", f"{count:,}"),
            ("Account age", rough_age(created, now_dt)),
            ("In server for", rough_age(joined, now_dt)),
            ("Account made", when(created)),
            ("Joined server", when(joined)),
            ("First seen", when(datetime.fromtimestamp(first, timezone.utc)) if first else "—"),
            ("Last seen", when(datetime.fromtimestamp(last, timezone.utc)) if last else "—"),
            ("Mod cases", str(len(cases))),
        ]
        stat_html = "".join(f"<div class='stat'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
                            for k, v in stats)
        intel_html = self._intel_html(guild, uid, member, created, now_dt)
        activity_html = self._activity_html(guild, uid)
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
            f"<div class='prof-grid'>{stat_html}</div>{intel_html}{activity_html}{chan_html}{restr_html}{hist_html}{actions}</div>"
            "<script>document.querySelectorAll('time[data-ts]').forEach(function(e){"
            "var d=new Date(+e.dataset.ts*1000);e.textContent=d.toLocaleDateString()+' '+"
            "d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});});</script>"
        )

    def _intel_html(self, guild: discord.Guild, uid: int, member, created, now_dt) -> str:
        """Discord-native intel: new-account flag, badges, perms, booster, invite, shared servers."""
        secs = []
        # Fresh-account warning — a useful alt/raid signal.
        if created is not None and (now_dt - created).days < 14:
            secs.append("<div class='note warn'>⚠ Account is less than 14 days old "
                        f"(created {rough_age(created, now_dt)} ago).</div>")
        flags = []
        pf = getattr(member, "public_flags", None)
        if pf is not None:
            with contextlib.suppress(Exception):
                flags = [BADGE_LABELS.get(f.name, f.name.replace('_', ' ').title()) for f in pf.all()]
        if getattr(member, "bot", False):
            flags.insert(0, "Bot")
        chips = "".join(f"<span class='chip'>{esc(b)}</span>" for b in flags)
        if chips:
            secs.append(f"<div class='prof-sec'><h3>Badges</h3><div class='chips'>{chips}</div></div>")
        # Key permissions in this server.
        perms = getattr(member, "guild_permissions", None)
        if perms is not None:
            held = [label for attr, label in KEY_PERMS if getattr(perms, attr, False)]
            if held:
                pc = "".join(f"<span class='chip perm'>{esc(p)}</span>" for p in held)
                secs.append(f"<div class='prof-sec'><h3>Key permissions</h3><div class='chips'>{pc}</div></div>")
        # Nitro boosting.
        boost = getattr(member, "premium_since", None)
        if boost is not None:
            secs.append(f"<div class='prof-sec'><h3>Boosting since</h3>"
                        f"<p><time data-ts='{int(boost.timestamp())}'></time> "
                        f"({rough_age(boost, now_dt)})</p></div>")
        # Who invited them (from SYKE's invite tracking).
        join = self.bot.storage.join_of(guild.id, uid)
        if join:
            inviter_id, inviter_name, code, kind, _ = join
            if kind == "vanity":
                who = f"vanity invite <code>/{esc(code or '')}</code>"
            elif inviter_id:
                who = f"<a href='/g/{guild.id}/u/{inviter_id}'>{esc(inviter_name or f'user {inviter_id}')}</a>"
                if code:
                    who += f" (<code>{esc(code)}</code>)"
            else:
                who = esc(inviter_name or "unknown")
            secs.append(f"<div class='prof-sec'><h3>Invited by</h3><p>{who}</p></div>")
        # Other servers SYKE shares with them — intel within the bot's own reach.
        mutual = [g for g in self.bot.guilds if g.id != guild.id and g.get_member(uid) is not None]
        if mutual:
            links = ", ".join(f"<a href='/g/{g.id}'>{esc(g.name)}</a>" for g in mutual[:15])
            more = f" +{len(mutual) - 15} more" if len(mutual) > 15 else ""
            secs.append(f"<div class='prof-sec'><h3>Also in ({len(mutual)})</h3><p>{links}{more}</p></div>")
        return "".join(secs)

    def _activity_html(self, guild: discord.Guild, uid: int) -> str:
        """A 24-hour activity heatmap (in the server's timezone) from stored message times."""
        tz = self.bot.tz_for(guild.id)
        hours = hour_histogram(self.bot.storage.member_post_times(guild.id, uid), tz)
        if not any(hours):
            return ""
        peak = peak_window(hours)
        top = max(hours) or 1
        bars = []
        for h in range(24):
            pct = int(round(hours[h] / top * 100))
            bars.append(f"<div class='hb' title='{hours[h]} msgs around {h:02d}:00'>"
                        f"<div class='hbf' style='height:{max(pct, 3)}%'></div><span>{h:02d}</span></div>")
        tzname = getattr(tz, "key", str(tz))
        note = (f"<p class='muted'>Most active around <strong>{peak[0]:02d}:00–{peak[1]:02d}:00</strong> "
                f"({esc(tzname)}).</p>" if peak else "")
        return (f"<div class='prof-sec'><h3>Activity by hour</h3>{note}"
                f"<div class='heat'>{''.join(bars)}</div></div>")

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
            f"<div class='dc'>{sidebar}"
            "<div class='dc-backdrop' onclick=\"this.closest('.dc').classList.remove('show-side')\"></div>"
            f"<div class='dc-main'>{head}{grid}</div></div>"
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

    async def message_reactions(self, request: web.Request) -> web.Response:
        """Who reacted to one message, grouped by emoji."""
        user = self._user(request)
        if user is None:
            raise web.HTTPForbidden(text="login required")
        guild = self._guild_or_404(request)
        channel = self._channel_or_404(request, guild)
        try:
            mid = int(request.match_info["mid"])
        except ValueError:
            raise web.HTTPNotFound(text="bad message id")
        try:
            message = await channel.fetch_message(mid)
        except discord.HTTPException:
            return web.Response(text="<div class='rxp-empty'>Message not found.</div>", content_type="text/html")
        return web.Response(text=await self._reactions_html(guild, message), content_type="text/html")

    def _embed_html(self, e, guild, names) -> str:
        """Render one embed the way Discord does: author, title, description, fields, media, footer."""
        parts = []
        # Author line (name + icon), e.g. the target user on a `,av` card.
        au = getattr(e, "author", None)
        if au is not None and getattr(au, "name", None):
            icon = getattr(au, "icon_url", None)
            ic = f"<img class='ea-ic' src='{esc(icon)}' alt=''>" if icon else ""
            aname = esc(au.name)
            aurl = getattr(au, "url", None)
            if aurl:
                aname = f"<a href='{esc(aurl)}' target='_blank' rel='noreferrer'>{aname}</a>"
            parts.append(f"<div class='ea'>{ic}<span>{aname}</span></div>")
        link = getattr(e, "url", None)
        title = esc(e.title)
        if title:
            parts.append(f"<a class='et' href='{esc(link)}' target='_blank' rel='noreferrer'>{title}</a>"
                         if link else f"<div class='et'>{title}</div>")
        if e.description:
            parts.append(f"<div class='ed'>{render_content(e.description[:1800], guild, names)}</div>")
        fields = getattr(e, "fields", []) or []
        if fields:
            fs = []
            for f in fields:
                inline = " inline" if getattr(f, "inline", False) else ""
                fs.append(f"<div class='ef{inline}'><div class='efn'>{esc(f.name)}</div>"
                          f"<div class='efv'>{render_content((f.value or '')[:800], guild, names)}</div></div>")
            parts.append(f"<div class='efs'>{''.join(fs)}</div>")
        video_url = getattr(getattr(e, "video", None), "url", None)
        image_url = getattr(getattr(e, "image", None), "url", None)
        thumb_url = getattr(getattr(e, "thumbnail", None), "url", None)
        if getattr(e, "type", "") == "gifv" and video_url:
            parts.append(f"<video class='emb-img' src='{esc(video_url)}' autoplay loop muted "
                         f"playsinline preload='metadata'></video>")
        elif image_url:
            parts.append(f"<img class='emb-img' src='{esc(image_url)}' loading='lazy' alt=''>")
        foot = getattr(e, "footer", None)
        ftext = getattr(foot, "text", None) if foot is not None else None
        ets = getattr(e, "timestamp", None)
        if ftext or ets:
            ficon = getattr(foot, "icon_url", None) if foot is not None else None
            fi = f"<img class='ef-ic' src='{esc(ficon)}' alt=''>" if ficon else ""
            stamp = f" · <time data-ts='{int(ets.timestamp())}'></time>" if ets else ""
            parts.append(f"<div class='eft'>{fi}<span>{esc(ftext or '')}{stamp}</span></div>")
        # Thumbnail sits top-right; only when it isn't already the main image.
        thumb = (f"<img class='eth' src='{esc(thumb_url)}' loading='lazy' alt=''>"
                 if thumb_url and thumb_url != image_url else "")
        if not parts and not thumb:
            return ""
        accent = getattr(getattr(e, "colour", None), "value", None)
        bar = f" style='border-left-color:#{accent:06x}'" if accent else ""
        inner = f"<div class='emb-main'>{''.join(parts)}</div>{thumb}" if thumb else "".join(parts)
        return f"<div class='emb'{bar}>{inner}</div>"

    def _components_html(self, components, guild, names) -> str:
        """Render a Components-V2 message (what many modern bots like .fmbot now send)."""
        return "".join(self._component_html(c, guild, names) for c in (components or []))

    def _component_html(self, c, guild, names) -> str:
        kind = _ctype(c)

        def media_url(media):
            return getattr(media, "url", None) or getattr(media, "proxy_url", None)

        if kind == "text_display":
            txt = getattr(c, "content", "") or ""
            return f"<div class='cv-text'>{render_content(txt[:2000], guild, names)}</div>" if txt.strip() else ""
        if kind == "thumbnail":
            u = media_url(getattr(c, "media", None))
            return (f"<a class='att cv-thumb' href='{esc(u)}' target='_blank' rel='noreferrer'>"
                    f"<img src='{esc(u)}' loading='lazy' alt=''></a>") if u else ""
        if kind == "media_gallery":
            imgs = []
            for it in getattr(c, "items", []) or []:
                u = media_url(getattr(it, "media", None))
                if u:
                    imgs.append(f"<a class='att' href='{esc(u)}' target='_blank' rel='noreferrer'>"
                                f"<img src='{esc(u)}' loading='lazy' alt=''></a>")
            return f"<div class='cv-gallery'>{''.join(imgs)}</div>" if imgs else ""
        if kind == "section":
            kids = "".join(self._component_html(k, guild, names) for k in getattr(c, "children", []) or [])
            acc = getattr(c, "accessory", None)
            acch = self._component_html(acc, guild, names) if acc is not None else ""
            return f"<div class='cv-section'><div class='cv-main'>{kids}</div>{acch}</div>"
        if kind == "container":
            kids = "".join(self._component_html(k, guild, names) for k in getattr(c, "children", []) or [])
            col = getattr(c, "accent_colour", None) or getattr(c, "accent_color", None)
            val = getattr(col, "value", None)
            bar = f" style='border-left-color:#{val:06x}'" if val else ""
            return f"<div class='cv-box'{bar}>{kids}</div>" if kids else ""
        if kind == "file":
            u = media_url(getattr(c, "media", None))
            name = getattr(c, "name", None) or "file"
            return (f"<a class='att att-file' href='{esc(u)}' target='_blank' rel='noreferrer'>"
                    f"<span class='fi'>📎</span><span class='fn'>{esc(name)}</span></a>") if u else ""
        if kind == "separator":
            return "<hr class='cv-sep'>"
        if kind == "action_row":
            return "".join(self._component_html(k, guild, names) for k in getattr(c, "children", []) or [])
        if kind == "button":
            label = getattr(c, "label", None) or ""
            url = getattr(c, "url", None)
            if not label and not url:
                return ""
            inner = esc(label or "link")
            return (f"<a class='cv-btn' href='{esc(url)}' target='_blank' rel='noreferrer'>{inner}</a>"
                    if url else f"<span class='cv-btn'>{inner}</span>")
        return ""

    async def _reactions_html(self, guild: discord.Guild, message: discord.Message) -> str:
        groups = []
        for reaction in getattr(message, "reactions", []) or []:
            e = reaction.emoji
            if getattr(e, "id", None):
                glyph = f"<img class='ie' src='{esc(getattr(e, 'url', ''))}' alt='{esc(getattr(e, 'name', ''))}'>"
            else:
                glyph = esc(str(e))
            chips = []
            try:
                async for u in reaction.users(limit=60):
                    name = getattr(u, "display_name", None) or getattr(u, "name", None) or f"User {u.id}"
                    av = getattr(getattr(u, "display_avatar", None), "url", "")
                    chips.append(
                        f"<a class='rxp-u' href='/g/{guild.id}/u/{u.id}'>"
                        f"<img src='{esc(av)}' alt='' loading='lazy'>{esc(name)}</a>")
            except discord.HTTPException:
                pass
            extra = f"<span class='muted'>+{reaction.count - len(chips)} more</span>" if reaction.count > len(chips) else ""
            groups.append(
                f"<div class='rxp-group'><div class='rxp-emoji'>{glyph}<span>{reaction.count}</span></div>"
                f"<div class='rxp-users'>{''.join(chips) or '<span class=muted>no one</span>'}{extra}</div></div>")
        if not groups:
            return "<div class='rxp-empty'>No reactions on this message.</div>"
        return "".join(groups)

    # Live updates (WebSocket)

    async def channel_ws(self, request: web.Request) -> web.WebSocketResponse:
        if self._user(request) is None:
            raise web.HTTPUnauthorized(text="login required")
        guild = self._guild_or_404(request)
        channel = self._channel_or_404(request, guild)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._subs[channel.id].add(ws)
        try:
            async for _ in ws:  # Keep the socket open; we only push, never read.
                pass
        finally:
            self._subs[channel.id].discard(ws)
        return ws

    async def _broadcast(self, channel_id: int, payload: dict) -> None:
        subs = self._subs.get(channel_id)
        if not subs:
            return
        for ws in list(subs):
            try:
                await ws.send_json(payload)
            except Exception:
                subs.discard(ws)

    async def _live_refs(self, message: discord.Message) -> tuple[dict, set]:
        """Resolve a single live message's reply target for the WS render."""
        ref = getattr(message, "reference", None)
        if ref is None or getattr(getattr(ref, "type", None), "name", "default") == "forward":
            return {}, set()
        mid = getattr(ref, "message_id", None)
        if mid is None or self._is_message(getattr(ref, "resolved", None)):
            return {}, set()
        try:
            return {mid: await message.channel.fetch_message(mid)}, set()
        except discord.NotFound:
            return {}, {mid}
        except discord.HTTPException:
            return {}, set()

    async def _on_new_message(self, message: discord.Message) -> None:
        if message.guild is None or message.channel.id not in self._subs:
            return
        with contextlib.suppress(Exception):
            refs, deleted = await self._live_refs(message)
            await self._broadcast(message.channel.id,
                                  {"t": "new", "mid": str(message.id),
                                   "html": self._one_message(message, None, refs, deleted)})

    async def _on_edit_message(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.channel.id not in self._subs:
            return
        with contextlib.suppress(Exception):
            refs, deleted = await self._live_refs(after)
            await self._broadcast(after.channel.id,
                                  {"t": "edit", "mid": str(after.id),
                                   "html": self._one_message(after, None, refs, deleted)})

    async def _on_delete_message(self, message: discord.Message) -> None:
        if message.guild is None or message.channel.id not in self._subs:
            return
        await self._broadcast(message.channel.id, {"t": "del", "mid": str(message.id)})

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
