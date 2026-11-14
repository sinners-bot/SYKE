import asyncio
import contextlib
import types
from dataclasses import replace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from syke import web_auth as auth
from syke.bot import Syke
from syke.config import load_settings
from syke.moderation import Moderation
from syke.web import WebDashboard, esc


SECRET = "unit-test-secret"


class Channel:
    def __init__(self, cid, name, *, read=True, send=True):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.topic = None
        self.guild = None
        self._read = read
        self._send = send
        self.sent = []
        self.sent_kwargs = []

    def permissions_for(self, who):
        return discord.Permissions(read_message_history=self._read, send_messages=self._send, view_channel=True)

    async def history(self, limit=50, before=None, around=None):
        msgs = list(getattr(self, "_history", []))
        if around is not None:
            # A jump window: just hand back everything (newest first) so the target is present.
            for m in sorted(msgs, key=lambda m: m.id, reverse=True)[:limit]:
                yield m
            return
        if before is not None:
            bid = getattr(before, "id", before)
            msgs = [m for m in msgs if m.id < bid]
        # Discord returns newest first.
        for m in sorted(msgs, key=lambda m: m.id, reverse=True)[:limit]:
            yield m

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        self.sent_kwargs.append(kwargs)
        return types.SimpleNamespace(id=1)

    async def fetch_message(self, mid):
        for m in getattr(self, "_history", []):
            if m.id == mid:
                return m
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "missing")


class Member:
    def __init__(self, uid):
        self.id = uid
        self.display_name = f"user{uid}"
        self.bot = False
        self.roles = []
        self.timed_out = "unset"
        self.kicked = False

    async def timeout(self, until, reason=None):
        self.timed_out = until

    async def kick(self, reason=None):
        self.kicked = True

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Guild:
    def __init__(self, gid=42):
        self.id = gid
        self.name = "Test Server"
        self.icon = None
        self.owner_id = 1
        self.owner = types.SimpleNamespace(display_name="Boss", id=1)
        self.me = types.SimpleNamespace(id=999)
        self.member_count = 10
        self.text_channels = [Channel(10, "general"), Channel(11, "locked", read=False, send=False)]
        self.channels = list(self.text_channels)
        for c in self.text_channels:
            c.guild = self
        self.members = {7: Member(7)}
        self.banned = []
        self.left = False

    def by_category(self):
        return [(None, list(self.text_channels))]

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    def get_channel_or_thread(self, cid):
        return self.get_channel(cid)

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid in self.members:
            return self.members[uid]
        raise discord.NotFound(types.SimpleNamespace(status=404, reason=""), "nope")

    async def ban(self, obj, reason=None, delete_message_seconds=0):
        self.banned.append(obj.id)

    async def leave(self):
        self.left = True


@pytest.fixture()
def dash(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "w.db"), ai_provider="none",
                       web_enabled=True, web_client_id="cid", web_client_secret="csec",
                       web_secret=SECRET, owner_ids=frozenset({42}))
    bot = Syke(settings)
    guild = Guild()
    bot._guild = guild
    bot.get_guild = lambda gid: guild if gid == guild.id else None
    bot.guilds_list = [guild]
    type(bot).guilds = property(lambda self: [guild])
    web = WebDashboard(bot)
    return web, bot, guild


class serving:
    """`async with serving(dash) as (cl, web, bot, guild):` — a started aiohttp test client."""

    def __init__(self, dash):
        self.web, self.bot, self.guild = dash

    async def __aenter__(self):
        self.cl = TestClient(TestServer(self.web.app))
        await self.cl.start_server()
        return self.cl, self.web, self.bot, self.guild

    async def __aexit__(self, *exc):
        await self.cl.close()


def _session_cookie(uid=42):
    return {auth.SESSION_COOKIE: auth.make_session(SECRET, uid, "Owner")}


async def test_home_requires_login(dash):
    async with serving(dash) as (cl, web, bot, guild):
        resp = await cl.get("/", allow_redirects=False)
        assert resp.status == 200
        assert "Log in with Discord" in await resp.text()


async def test_login_redirects_to_discord(dash):
    async with serving(dash) as (cl, web, bot, guild):
        resp = await cl.get("/login", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"].startswith(auth.OAUTH_AUTHORIZE)


async def test_non_owner_session_is_rejected(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: auth.make_session(SECRET, 999, "x")})
        resp = await cl.get("/", allow_redirects=False)
        assert "Log in with Discord" in await resp.text()  # treated as logged out


async def test_owner_sees_server_list(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.get("/")
        body = await resp.text()
        assert "Test Server" in body and "1 servers" in body


async def test_server_page_and_channel_links(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.get(f"/g/{guild.id}")
        body = await resp.text()
        assert "#general" in body and "Moderate" in body and "Leave server" in body


async def test_say_requires_csrf(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        # No CSRF token -> 403.
        resp = await cl.post(f"/g/{guild.id}/c/10/say", data={"content": "hi"}, allow_redirects=False)
        assert resp.status == 403
        # With CSRF -> sends.
        token = auth.csrf_token(SECRET, 42)
        resp = await cl.post(f"/g/{guild.id}/c/10/say", data={"content": "hello chat", "csrf": token},
                             allow_redirects=False)
        assert resp.status == 302
        assert guild.get_channel(10).sent == ["hello chat"]


async def test_moderation_timeout_and_kick(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        token = auth.csrf_token(SECRET, 42)
        resp = await cl.post(f"/g/{guild.id}/mod",
                             data={"user_id": "7", "action": "timeout", "duration": "10m", "csrf": token},
                             allow_redirects=False)
        assert resp.status == 302
        assert guild.members[7].timed_out is not None and guild.members[7].timed_out != "unset"
        assert bot.storage.cases(guild.id, 7, "timeout")

        resp = await cl.post(f"/g/{guild.id}/mod", data={"user_id": "7", "action": "kick", "csrf": token},
                             allow_redirects=False)
        assert guild.members[7].kicked


async def test_moderation_restrict_writes_rows(dash):
    async with serving(dash) as (cl, web, bot, guild):
        await bot.add_cog(Moderation(bot))
        bot.get_cog("Moderation").expire.cancel()
        # ensure_role needs guild.create_role / get_role
        created = {}
        guild.get_role = lambda rid: created.get(rid)
        async def create_role(*, name, permissions, mentionable, reason):
            r = types.SimpleNamespace(id=500 + len(created), name=name, position=1, mention="<@&1>")
            created[r.id] = r
            return r
        guild.create_role = create_role
        guild.channels = guild.text_channels
        for ch in guild.text_channels:
            ch.overwrites_for = lambda role: discord.PermissionOverwrite()
            ch.set_permissions = lambda *a, **k: _noop()
        cl.session.cookie_jar.update_cookies(_session_cookie())
        token = auth.csrf_token(SECRET, 42)
        resp = await cl.post(f"/g/{guild.id}/mod",
                             data=[("user_id", "7"), ("action", "restrict"), ("kind", "images"),
                                   ("kind", "links"), ("csrf", token)], allow_redirects=False)
        assert resp.status == 302
        kinds = {row[1] for row in bot.storage.restrictions(guild.id, 7)}
        assert kinds == {"images", "links"}


async def test_change_prefix_and_leave(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        token = auth.csrf_token(SECRET, 42)
        await cl.post(f"/g/{guild.id}/settings",
                      data={"field": "bulk", "prefix": "?", "timezone": "Europe/London", "csrf": token},
                      allow_redirects=False)
        assert bot.prefix_for(guild.id) == "?"
        assert bot.storage.timezone(guild.id) == "Europe/London"
        await cl.post(f"/g/{guild.id}/leave", data={"csrf": token}, allow_redirects=False)
        assert guild.left


async def test_clear_data(dash):
    async with serving(dash) as (cl, web, bot, guild):
        bot.storage.add_corpus(guild.id, [(1, 10, 7, "hi there friend", 1.0, "user7", 0, 0)])
        cl.session.cookie_jar.update_cookies(_session_cookie())
        token = auth.csrf_token(SECRET, 42)
        await cl.post(f"/g/{guild.id}/clear", data={"csrf": token}, allow_redirects=False)
        assert bot.storage.corpus_size(guild.id)[0] == 0


async def test_channel_view_renders_discord_style(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.get(f"/g/{guild.id}/c/10")
        assert resp.status == 200
        body = await resp.text()
        assert "dc-side" in body and "dc-msgs" in body          # sidebar + message pane
        assert "# general" in body or "#</span> general" in body  # channel header
        assert "Message #general as SYKE" in body                 # input bar
        assert "new WebSocket(" in body and "/ws'" in body        # live websocket


def _fake_message(mid, uid, name, content, ts):
    import types
    from datetime import datetime, timezone
    author = types.SimpleNamespace(id=uid, display_name=name, bot=False,
        display_avatar=types.SimpleNamespace(url=f"https://cdn/{uid}.png"),
        colour=types.SimpleNamespace(value=0))
    return types.SimpleNamespace(id=mid, author=author, clean_content=content, content=content, guild=None, stickers=[],
        created_at=datetime.fromtimestamp(ts, timezone.utc), reference=None,
        attachments=[], embeds=[], reactions=[])


async def test_history_paging_with_before(dash):
    async with serving(dash) as (cl, web, bot, guild):
        ch = guild.get_channel(10)
        ch._history = [_fake_message(100 + i, 7, "Ada", f"message {i}", 1_700_000_000 + i) for i in range(10)]
        cl.session.cookie_jar.update_cookies(_session_cookie())
        # Latest page: no 'before'
        latest = await (await cl.get(f"/g/{guild.id}/c/10/messages")).text()
        assert "data-mid='109'" in latest and "setInterval" not in latest
        # Older page: before the oldest shown
        older = await (await cl.get(f"/g/{guild.id}/c/10/messages?before=105")).text()
        assert "data-mid='104'" in older and "data-mid='105'" not in older
        # The channel page wires up scroll-back
        page = await (await cl.get(f"/g/{guild.id}/c/10")).text()
        assert "?before='+f.dataset.mid" in page and "addEventListener('scroll'" in page


def _comp(kind, **kw):
    return types.SimpleNamespace(type=types.SimpleNamespace(name=kind), **kw)


def _media(url):
    return types.SimpleNamespace(url=url, proxy_url=None)


def _fmbot_card():
    text = _comp("text_display", content="**Now playing** Song — Artist")
    thumb = _comp("thumbnail", media=_media("https://cdn/art.png"))
    section = _comp("section", children=[text], accessory=thumb)
    gallery = _comp("media_gallery", items=[types.SimpleNamespace(media=_media("https://cdn/g1.png"))])
    button = _comp("button", label="Open in Spotify", url="https://open.spotify.com/x")
    row = _comp("action_row", children=[button])
    return _comp("container", children=[section, gallery, row],
                 accent_colour=types.SimpleNamespace(value=0x1DB954))


def test_components_v2_render(dash):
    web, bot, guild = dash
    html = web._components_html([_fmbot_card()], guild, {})
    assert "Now playing" in html                 # text display
    assert "https://cdn/art.png" in html         # section thumbnail (the album art)
    assert "https://cdn/g1.png" in html          # media gallery
    assert "Open in Spotify" in html             # button label
    assert "border-left-color:#1db954" in html   # container accent


def _snapshot(content="", atts=(), embeds=(), stickers=(), comps=()):
    return types.SimpleNamespace(content=content, attachments=list(atts), embeds=list(embeds),
                                 stickers=list(stickers), components=list(comps))


def test_forwarded_message_renders_snapshot(dash):
    web, bot, guild = dash
    m = _fake_message(401, 7, "Charlie", "", 1_700_000_000)
    m.guild = guild
    # Discord forward: a forward-type reference + the real content in message_snapshots.
    m.reference = types.SimpleNamespace(type=types.SimpleNamespace(name="forward"),
                                        message_id=99, resolved=None)
    m.message_snapshots = [_snapshot(content="the forwarded text")]
    html = web._one_message(m, None)
    assert "Forwarded" in html and "the forwarded text" in html
    assert "(no text content)" not in html
    assert "Original message was deleted" not in html   # a forward is not a deletion


def test_unresolved_reply_is_not_called_deleted(dash):
    web, bot, guild = dash
    m = _fake_message(402, 7, "Ada", "hi", 1_700_000_000)
    m.guild = guild
    m.channel = types.SimpleNamespace(id=10)
    m.reference = types.SimpleNamespace(message_id=12345, resolved=None)  # no type -> normal reply
    # Not in refs and not in the deleted set -> neutral stub, not a false "deleted".
    html = web._one_message(m, None, refs={}, deleted=set())
    assert "replying to a message" in html
    assert "Original message was deleted" not in html
    # When we *know* it's deleted, say so.
    html2 = web._one_message(m, None, refs={}, deleted={12345})
    assert "Original message was deleted" in html2


def test_one_message_shows_component_card_not_empty(dash):
    web, bot, guild = dash
    m = _fake_message(301, 7, "fmbot", "", 1_700_000_000)
    m.guild = guild
    m.components = [_fmbot_card()]
    html = web._one_message(m, None)
    assert "(no text content)" not in html
    assert "Now playing" in html and "https://cdn/art.png" in html


def test_result_text_uses_component_text(dash):
    web, bot, guild = dash
    m = _fake_message(1, 7, "b", "", 1)
    m.components = [_comp("container", children=[_comp("text_display", content="Now playing X")])]
    assert web._result_text(m) == "Now playing X"


def test_parse_search_operators():
    from syke.web import parse_search
    f = parse_search("from:<@111111111111111111> has:image has:video before:2025-06-01 hello world")
    assert 111111111111111111 in f.from_ids
    assert f.has == {"image", "video"}
    assert f.before is not None and f.after is None
    assert f.text == "hello world"
    assert f.structural and f.has_from
    g = parse_search("from:Ada just text")
    assert g.from_names == ["ada"] and g.text == "just text" and not g.structural
    assert parse_search("plain words").text == "plain words"
    assert parse_search("<@!999> hi") .text == "<@!999> hi"   # bare mention (no from:) is text
    h = parse_search("mentions:<@222222222222222222> in:#general hi")
    assert 222222222222222222 in h.mention_ids and h.in_names == ["general"] and h.has_in and h.text == "hi"
    assert 333333333333333333 in parse_search("in:<#333333333333333333>").in_ids


def _smsg(uid=7, name="Ada", content="", atts=(), embeds=(), stickers=(), ts=1_700_000_000):
    from datetime import datetime, timezone
    def att(ctype, fn="f"):
        return types.SimpleNamespace(content_type=ctype, filename=fn, url=f"https://cdn/{fn}")
    return types.SimpleNamespace(
        id=1, author=types.SimpleNamespace(id=uid, display_name=name, name=name),
        content=content, clean_content=content,
        attachments=[att(*a) if isinstance(a, tuple) else att(a) for a in atts],
        embeds=list(embeds), stickers=list(stickers),
        created_at=datetime.fromtimestamp(ts, timezone.utc))


def test_message_matches_filters():
    from syke.web import message_matches, parse_search
    img = _smsg(content="look at this", atts=[("image/png", "a.png")])
    assert message_matches(img, parse_search("has:image"))
    assert message_matches(img, parse_search("has:image look"))
    assert not message_matches(img, parse_search("has:video"))
    assert not message_matches(img, parse_search("has:image missing"))   # text must also match
    # from: by name and id
    assert message_matches(img, parse_search("from:ada"))
    assert not message_matches(img, parse_search("from:bob"))
    assert message_matches(_smsg(uid=111111111111111111), parse_search("from:111111111111111111"))
    # link detection from content
    assert message_matches(_smsg(content="see https://x.com"), parse_search("has:link"))
    # date window
    early = _smsg(ts=1_600_000_000)
    assert message_matches(early, parse_search("before:2021-01-01"))
    assert not message_matches(early, parse_search("after:2021-01-01"))
    # mentions:
    pinged = _smsg(content="hey")
    pinged.mentions = [types.SimpleNamespace(id=222222222222222222, display_name="Bob", name="Bob")]
    assert message_matches(pinged, parse_search("mentions:222222222222222222"))
    assert message_matches(pinged, parse_search("mentions:bob"))
    assert not message_matches(pinged, parse_search("mentions:alice"))
    # has:image from a component media gallery (fmbot-style card)
    carded = _smsg()
    carded.components = [_comp("container", children=[_comp("media_gallery",
                        items=[types.SimpleNamespace(media=_media("https://cdn/x.png"))])])]
    assert message_matches(carded, parse_search("has:image"))


def _embed(**kw):
    base = dict(title=None, description=None, url=None, author=None, fields=[], footer=None,
                timestamp=None, image=None, thumbnail=None, video=None, type="rich", colour=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_embed_html_renders_bot_card_fully(dash):
    web, bot, guild = dash
    e = _embed(
        title="User Info", description="an account",
        author=types.SimpleNamespace(name="Ada", icon_url="https://cdn/ic.png", url=None),
        fields=[types.SimpleNamespace(name="ID", value="123", inline=True),
                types.SimpleNamespace(name="Created", value="2020", inline=True)],
        footer=types.SimpleNamespace(text="bleed", icon_url=None),
        image=types.SimpleNamespace(url="https://cdn/img.png"),
        thumbnail=types.SimpleNamespace(url="https://cdn/th.png"),
        colour=types.SimpleNamespace(value=0xFF8800),
    )
    html = web._embed_html(e, guild, {})
    assert "Ada" in html and "https://cdn/ic.png" in html        # author + icon
    assert "User Info" in html                                   # title
    assert "ID" in html and "123" in html and "Created" in html  # fields
    assert "bleed" in html                                       # footer
    assert "https://cdn/img.png" in html                         # image
    assert "https://cdn/th.png" in html                          # thumbnail
    assert "border-left-color:#ff8800" in html                   # colour bar


def test_embed_html_avatar_card_without_title(dash):
    web, bot, guild = dash
    e = _embed(author=types.SimpleNamespace(name="Ada avatar", icon_url=None, url=None),
               image=types.SimpleNamespace(url="https://cdn/av.png"))
    html = web._embed_html(e, guild, {})
    assert "Ada avatar" in html and "https://cdn/av.png" in html


def test_embed_html_empty_is_skipped(dash):
    web, bot, guild = dash
    assert web._embed_html(_embed(), guild, {}) == ""


def test_install_permissions_requires_administrator():
    from syke.bot import install_permissions
    assert install_permissions().administrator is True


def test_announce_body_excludes_owner_features():
    from syke.web import ANNOUNCE_BODY
    low = ANNOUNCE_BODY.lower()
    assert "dashboard" not in low and "owner" not in low   # never leak owner-only stuff
    assert "moderation" in low and "text toys" in low      # but does cover public features


async def test_search_jump_link_is_in_dashboard(dash):
    async with serving(dash) as (cl, web, bot, guild):
        guild.get_channel(10)._history = [_fake_message(500, 7, "Ada", "hello world here", 1_700_000_000)]
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10?q=hello")).text()
        assert f"/g/{guild.id}/c/10?to=500" in body      # internal jump
        assert "Jump →" in body
        assert "discord.com/channels" not in body         # no external redirect


async def test_channel_jump_sets_target(dash):
    async with serving(dash) as (cl, web, bot, guild):
        guild.get_channel(10)._history = [_fake_message(600, 7, "Ada", "hi", 1_700_000_000)]
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10?to=600")).text()
        assert "var JUMP=600;" in body


async def test_announce_posts_embed(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        token = auth.csrf_token(SECRET, 42)
        resp = await cl.post(f"/g/{guild.id}/announce",
                             data={"cid": "10", "title": "Big news", "body": "we shipped stuff", "csrf": token},
                             allow_redirects=False)
        assert resp.status == 302
        kwargs = guild.get_channel(10).sent_kwargs[-1]
        em = kwargs.get("embed")
        assert em is not None and em.title == "Big news" and "we shipped stuff" in em.description


def test_dashboard_css_is_mobile_friendly():
    from syke.web import STYLE
    assert "@media(max-width:820px)" in STYLE            # phone breakpoint exists
    assert ".dc-burger" in STYLE and ".dc.show-side .dc-side" in STYLE  # slide-in channel drawer


def test_channel_header_has_drawer_toggle(dash):
    web, bot, guild = dash
    h = web._channel_header(guild, guild.get_channel(10), "chat")
    assert "dc-burger" in h and "show-side" in h          # hamburger toggles the sidebar


def test_hour_histogram_and_peak_window():
    from datetime import datetime, timezone

    from syke.web import hour_histogram, peak_window
    tz = timezone.utc
    t14 = datetime(2026, 1, 1, 14, 0, tzinfo=tz).timestamp()
    t15 = datetime(2026, 1, 1, 15, 0, tzinfo=tz).timestamp()
    h = hour_histogram([t14, t14, t15], tz)
    assert h[14] == 2 and h[15] == 1 and sum(h) == 3
    assert peak_window([0] * 24) is None
    pk = peak_window(h)
    assert pk is not None and pk[0] in (13, 14)


def test_join_clusters_groups_bursts():
    from syke.web import join_clusters
    joins = [
        (1, None, "", "", "", 100.0), (2, None, "", "", "", 110.0), (3, None, "", "", "", 150.0),
        (9, None, "", "", "", 5000.0),   # lone, far apart
    ]
    assert join_clusters(joins, window=60, min_size=3) == [[1, 2, 3]]
    assert join_clusters(joins, window=5, min_size=3) == []   # too strict a window


def test_activity_html_renders_heatmap(dash):
    web, bot, guild = dash
    from datetime import datetime, timezone
    base = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc).timestamp()
    bot.storage.member_post_times = lambda gid, uid, limit=5000: [base] * 6
    html = web._activity_html(guild, 7)
    assert "Activity by hour" in html and "hbf" in html and "Most active" in html


def test_activity_html_empty_when_no_data(dash):
    web, bot, guild = dash
    bot.storage.member_post_times = lambda gid, uid, limit=5000: []
    assert web._activity_html(guild, 7) == ""


def test_alts_body_flags_new_accounts_bursts_and_inviters(dash):
    web, bot, _ = dash
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def mem(uid, days, avatar=True):
        return types.SimpleNamespace(id=uid, bot=False, display_name=f"u{uid}",
                                     created_at=now - timedelta(days=days),
                                     avatar=(object() if avatar else None),
                                     display_avatar=types.SimpleNamespace(url=f"https://cdn/{uid}.png"))

    members = [mem(1, 2, avatar=False), mem(2, 5), mem(3, 1000)]
    g = types.SimpleNamespace(id=42, name="G", members=members,
                              get_member=lambda uid: next((m for m in members if m.id == uid), None))
    joins = [(10, 99, "Inv", "abc", "normal", 1000.0), (11, 99, "Inv", "abc", "normal", 1010.0),
             (12, 99, "Inv", "abc", "normal", 1020.0)]
    bot.storage.joins_all = lambda gid: joins
    html = web._alts_body(g)
    assert "Newest accounts" in html and "no avatar" in html       # u1 (2d, no avatar)
    assert "u2" in html and "u3" not in html                       # young flagged, 1000d-old account isn't
    assert "Join bursts" in html and "3 joined together" in html
    assert "Top inviters" in html and "Inv" in html


def _reaction(emoji, who, count=None):
    async def users_fn(limit=100):
        for u in who:
            yield u
    return types.SimpleNamespace(emoji=emoji, count=count if count is not None else len(who), users=users_fn)


def _reactor(uid, name):
    return types.SimpleNamespace(id=uid, display_name=name,
                                 display_avatar=types.SimpleNamespace(url=f"https://cdn/{uid}.png"))


async def test_reactions_html_lists_who_reacted(dash):
    web, bot, guild = dash
    custom = types.SimpleNamespace(id=5, url="https://cdn/emo.png", name="pepe")
    msg = types.SimpleNamespace(reactions=[
        _reaction("🔥", [_reactor(7, "Ada"), _reactor(8, "Bo")]),
        _reaction(custom, [_reactor(7, "Ada")], count=3),
    ])
    html = await web._reactions_html(guild, msg)
    assert "Ada" in html and "Bo" in html
    assert "/g/42/u/7" in html and "/g/42/u/8" in html   # reactors link to profiles
    assert "🔥" in html and "https://cdn/emo.png" in html  # unicode + custom emoji
    assert "+2 more" in html                              # count 3, one user shown


async def test_reactions_html_empty(dash):
    web, bot, guild = dash
    html = await web._reactions_html(guild, types.SimpleNamespace(reactions=[]))
    assert "No reactions" in html


def test_one_message_reactions_are_clickable(dash):
    web, bot, guild = dash
    m = _fake_message(201, 7, "Ada", "hi", 1_700_000_000)
    m.guild = guild
    m.reactions = [_reaction("🔥", [], count=4)]
    html = web._one_message(m, None)
    assert "rx rxc" in html and "class='rxs'" in html and "See who reacted" in html


def test_rough_age():
    from datetime import datetime, timedelta, timezone

    from syke.web import rough_age
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert rough_age(now - timedelta(hours=3), now) == "today"
    assert rough_age(now - timedelta(days=5), now) == "5 days"
    assert rough_age(now - timedelta(days=60), now) == "2 months"
    assert rough_age(now - timedelta(days=800), now).startswith("2y")
    assert rough_age(None, now) == "—"


def test_intel_html_surfaces_badges_perms_and_new_account(dash):
    web, bot, guild = dash
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=3)   # fresh account
    pf = types.SimpleNamespace(all=lambda: [types.SimpleNamespace(name="active_developer"),
                                            types.SimpleNamespace(name="early_supporter")])
    perms = types.SimpleNamespace(administrator=True)   # other KEY_PERMS default False via getattr
    member = types.SimpleNamespace(public_flags=pf, bot=False, guild_permissions=perms, premium_since=None)
    html = web._intel_html(guild, 7, member, created, now)
    assert "less than 14 days" in html
    assert "Active Developer" in html and "Early Supporter" in html
    assert "Administrator" in html
    assert "Also in" not in html        # bot shares no other server in this fixture


def _voice_member(uid, name, **vstate):
    voice = types.SimpleNamespace(mute=False, self_mute=False, deaf=False, self_deaf=False,
                                  self_stream=False, self_video=False)
    for k, v in vstate.items():
        setattr(voice, k, v)
    return types.SimpleNamespace(
        id=uid, display_name=name, voice=voice,
        display_avatar=types.SimpleNamespace(url=f"https://cdn/{uid}.png"))


def test_voice_block_lists_members_with_mute_state(dash):
    web, bot, guild = dash
    vc = types.SimpleNamespace(id=55, name="General Voice", members=[
        _voice_member(1, "Ada", self_mute=True),
        _voice_member(2, "Bo", self_deaf=True),
        _voice_member(3, "Cy", self_stream=True),
        _voice_member(4, "Di"),  # connected, unmuted
    ])
    html = web._voice_block(guild, vc)
    assert "General Voice" in html and ">4<" in html            # name + member count
    assert "Ada" in html and "Bo" in html and "Di" in html      # everyone shown
    assert "Muted" in html and "Deafened" in html and "LIVE" in html
    assert "/g/42/u/1" in html                                   # clickable to profile
    assert html.count("vc-m") >= 4                               # a row each


def test_voice_block_empty_channel_has_no_member_list(dash):
    web, bot, guild = dash
    vc = types.SimpleNamespace(id=56, name="AFK", members=[])
    html = web._voice_block(guild, vc)
    assert "AFK" in html and "vc-members" not in html


def _reply_to(msg, target_id, resolved=None):
    msg.reference = types.SimpleNamespace(message_id=target_id, resolved=resolved)
    return msg


async def test_reply_preview_shows_referenced_message(dash):
    async with serving(dash) as (cl, web, bot, guild):
        ch = guild.get_channel(10)
        orig = _fake_message(200, 7, "Ada", "the original question", 1_700_000_000)
        reply = _reply_to(_fake_message(201, 8, "Bob", "here is the answer", 1_700_000_050), 200)
        deleted = _reply_to(_fake_message(202, 8, "Bob", "answering a ghost", 1_700_000_060), 999)
        ch._history = [orig, reply, deleted]
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10/messages")).text()
        # The reply points at the original and shows its author + a snippet.
        assert "data-jump='200'" in body
        assert "the original question" in body
        assert "Ada" in body
        # A reply to a message we can't resolve says so instead of crashing.
        assert "Original message was deleted" in body


async def test_resolve_refs_fetches_offpage_targets(dash):
    web, bot, guild = dash
    ch = guild.get_channel(10)
    orig = _fake_message(300, 7, "Ada", "older message", 1_700_000_000)
    reply = _reply_to(_fake_message(301, 8, "Bob", "reply", 1_700_000_050), 300)
    ch._history = [orig]  # original is fetchable but NOT in the page batch
    refs, deleted = await web._resolve_refs(ch, [reply])
    assert 300 in refs and refs[300].id == 300
    # A resolved payload is used directly without any fetch.
    reply2 = _reply_to(_fake_message(303, 8, "Bob", "r2", 1_700_000_060), 302, resolved=orig)
    refs2, _ = await web._resolve_refs(ch, [reply2])
    assert refs2[302].id == 300
    # A reply to a truly missing message is reported as deleted, not fetched forever.
    reply3 = _reply_to(_fake_message(305, 8, "Bob", "r3", 1_700_000_070), 999999)
    _, gone = await web._resolve_refs(ch, [reply3])
    assert 999999 in gone


async def test_channel_messages_endpoint(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.get(f"/g/{guild.id}/c/10/messages")
        assert resp.status == 200
        # Anonymous is refused.
        cl.session.cookie_jar.clear()
        resp = await cl.get(f"/g/{guild.id}/c/10/messages", allow_redirects=False)
        assert resp.status == 403


async def test_sidebar_has_search_and_media_tab(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10")).text()
        assert "id='chsearch'" in body and "id='chlist'" in body   # channel search box
        assert "/c/10/media" in body and ">Media<" in body          # media tab
        assert "chsearch" in body


async def test_media_endpoint(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.get(f"/g/{guild.id}/c/10/media")
        assert resp.status == 200
        assert "media-grid" in (await resp.text()) or "No media" in (await resp.text())


async def test_channel_message_search(dash):
    async with serving(dash) as (cl, web, bot, guild):
        bot.storage.track(guild.id, 10)
        now = 1_700_000_000.0
        bot.storage.add_corpus(guild.id, [
            (1001, 10, 7, "the quick brown fox jumps", now, "Ada", 0, 0),
            (1002, 10, 7, "nothing to see here", now + 1, "Ada", 0, 0),
            (1003, 10, 8, "another FOX appeared", now + 2, "Bo", 0, 0),
        ])
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10?q=fox")).text()
        assert "2 result(s) for" in body
        assert "<mark>" in body                    # highlighted match
        assert "quick brown" in body and "another" in body
        assert "nothing to see" not in body
        assert f"/g/{guild.id}/c/10?to=1001" in body   # in-dashboard jump link
        # value prefilled in the search box, poll disabled
        assert "value='fox'" in body and "setInterval(poll" not in body


def test_storage_search_escapes_wildcards(cog):
    now = 1.0
    cog.bot.storage.add_corpus(42, [
        (1, 5, 7, "100% sure about this", now, "Ada", 0, 0),
        (2, 5, 7, "fifty percent maybe", now + 1, "Ada", 0, 0),
    ])
    hits = cog.bot.storage.search_channel(42, 5, "100%")
    assert len(hits) == 1 and hits[0][0] == 1
    assert cog.bot.storage.search_channel(42, 5, "") == []


def test_attachment_previews_render_inline(dash):
    import types
    from datetime import datetime, timezone
    web = dash[0]
    def att(ct, url, name, size=1000):
        return types.SimpleNamespace(content_type=ct, url=url, filename=name, size=size)
    def emb(**k):
        base = dict(type="rich", url=None, title=None, description=None,
                    image=types.SimpleNamespace(url=None), thumbnail=types.SimpleNamespace(url=None),
                    video=types.SimpleNamespace(url=None))
        base.update(k)
        return types.SimpleNamespace(**base)
    author = types.SimpleNamespace(id=1, display_name="Ada", bot=False,
        display_avatar=types.SimpleNamespace(url="https://cdn/a.png"), colour=types.SimpleNamespace(value=0))
    m = types.SimpleNamespace(id=9, author=author, clean_content="", content="", guild=None, stickers=[], created_at=datetime(2026,11,13,tzinfo=timezone.utc),
        reference=None, reactions=[],
        attachments=[att("image/png","https://cdn/i.png","p.png"), att("video/mp4","https://cdn/v.mp4","v.mp4"),
                     att("audio/mpeg","https://cdn/s.mp3","s.mp3"), att("application/pdf","https://cdn/d.pdf","doc.pdf")],
        embeds=[emb(type="gifv", video=types.SimpleNamespace(url="https://cdn/g.mp4")),
                emb(title="Hello", image=types.SimpleNamespace(url="https://cdn/e.png"))])
    html = web._one_message(m, None)
    assert "att-media" in html and "src='https://cdn/v.mp4'" in html   # inline video
    assert "<audio" in html and "s.mp3" in html                        # inline audio
    assert "att-file" in html and "doc.pdf" in html                    # file card
    assert "emb-img" in html and "https://cdn/e.png" in html           # embed image
    assert "autoplay loop muted" in html and "https://cdn/g.mp4" in html  # gif as video


async def test_member_profile_page(dash):
    async with serving(dash) as (cl, web, bot, guild):
        bot.storage.add_corpus(guild.id, [
            (1, 10, 7, "hello world one", 1_700_000_000.0, "user7", 0, 0),
            (2, 10, 7, "hello world two", 1_700_000_100.0, "user7", 0, 0),
        ])
        bot.storage.add_case(guild.id, 7, 2, "warn", "being rude")
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/u/7")).text()
        assert "prof-top" in body                    # profile header
        assert "Stored messages" in body and "2" in body
        assert "Moderation history" in body and "being rude" in body
        assert f"action='/g/{guild.id}/mod'" in body  # quick actions prefilled
        assert "value='7'" in body


async def test_chat_has_member_list_and_clickable_authors(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/c/10")).text()
        assert "dc-members" in body                   # right-hand member column
        assert f"/g/{guild.id}/u/7" in body           # member row links to profile


async def test_emoji_gallery(dash):
    import types
    async with serving(dash) as (cl, web, bot, guild):
        guild.emojis = [types.SimpleNamespace(name="pepe", url="https://cdn/e1.png", animated=False),
                        types.SimpleNamespace(name="dance", url="https://cdn/e2.gif", animated=True)]
        guild.stickers = []
        cl.session.cookie_jar.update_cookies(_session_cookie())
        body = await (await cl.get(f"/g/{guild.id}/emojis")).text()
        assert "Emojis (2)" in body
        assert ":pepe:" in body and ":dance:" in body and "GIF" in body
        assert "https://cdn/e1.png" in body
        # linked from the server page
        page = await (await cl.get(f"/g/{guild.id}")).text()
        assert f"/g/{guild.id}/emojis" in page


def test_inline_emoji_mentions_and_stickers(dash):
    import types
    from datetime import datetime, timezone
    web = dash[0]
    guild = types.SimpleNamespace(id=42,
        get_member=lambda i: types.SimpleNamespace(display_name="Ada") if i == 7 else None,
        get_channel=lambda i: types.SimpleNamespace(name="general") if i == 10 else None,
        get_role=lambda i: None)
    author = types.SimpleNamespace(id=1, display_name="Bo", bot=False,
        display_avatar=types.SimpleNamespace(url="x"), colour=types.SimpleNamespace(value=0))
    react = types.SimpleNamespace(id=9, url="https://cdn/r.png", name="pepe")
    m = types.SimpleNamespace(id=5, guild=guild, author=author,
        content="yo <:pog:123> <@7> see <#10>", clean_content="",
        created_at=datetime(2026, 11, 13, tzinfo=timezone.utc), reference=None,
        attachments=[], embeds=[], stickers=[types.SimpleNamespace(name="catjam", url="https://cdn/s.png")],
        reactions=[types.SimpleNamespace(emoji=react, count=3)])
    html = web._one_message(m, None)
    assert "cdn.discordapp.com/emojis/123.png" in html     # custom emoji inline
    assert "class='mention'>@Ada" in html                  # user mention chip
    assert "class='mention'>#general" in html              # channel mention chip
    assert "class='sticker'" in html and "https://cdn/s.png" in html  # sticker image
    assert "class='ie' src='https://cdn/r.png'" in html    # custom-emoji reaction as image


async def test_live_websocket_pushes_messages(dash):
    import types
    from datetime import datetime, timezone
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        ws = await cl.ws_connect(f"/g/{guild.id}/c/10/ws")
        ch = guild.get_channel(10)
        author = types.SimpleNamespace(id=7, display_name="Ada", bot=False,
            display_avatar=types.SimpleNamespace(url="a"), colour=types.SimpleNamespace(value=0))
        msg = types.SimpleNamespace(id=777, guild=guild, channel=ch, author=author, content="live now",
            clean_content="live now", created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
            reference=None, attachments=[], embeds=[], stickers=[], reactions=[])
        await web._on_new_message(msg)
        data = await asyncio.wait_for(ws.receive_json(), timeout=2)
        assert data["t"] == "new" and data["mid"] == "777" and "live now" in data["html"]
        await web._on_delete_message(msg)
        gone = await asyncio.wait_for(ws.receive_json(), timeout=2)
        assert gone["t"] == "del" and gone["mid"] == "777"
        await ws.close()


async def test_websocket_requires_login(dash):
    async with serving(dash) as (cl, web, bot, guild):
        try:
            ws = await cl.ws_connect(f"/g/{guild.id}/c/10/ws")
            await ws.close()
            assert False, "should have refused"
        except Exception:
            pass


def test_mentions_resolve_without_members_intent(dash):
    import types
    from datetime import datetime, timezone
    web = dash[0]
    # guild cache is empty (Members intent off) — name must come from the message payload.
    guild = types.SimpleNamespace(id=42, get_member=lambda i: None, get_channel=lambda i: None,
                                  get_role=lambda i: None)
    author = types.SimpleNamespace(id=1, display_name="Bo", bot=False,
        display_avatar=types.SimpleNamespace(url="x"), colour=types.SimpleNamespace(value=0))
    pinged = types.SimpleNamespace(id=999, display_name="CoolGuy", name="coolguy")
    m = types.SimpleNamespace(id=5, guild=guild, author=author, content="yo <@999> look",
        clean_content="", created_at=datetime(2026, 11, 13, tzinfo=timezone.utc), reference=None,
        attachments=[], embeds=[], stickers=[], reactions=[],
        mentions=[pinged], channel_mentions=[], role_mentions=[])
    html = web._one_message(m, None)
    assert "@CoolGuy" in html
    assert ">@999<" not in html   # the raw id is not shown as the mention


def test_html_escaping_blocks_injection():
    assert esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


async def _noop():
    return None
