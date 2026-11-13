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

    def permissions_for(self, who):
        return discord.Permissions(read_message_history=self._read, send_messages=self._send, view_channel=True)

    async def history(self, limit=50, before=None):
        msgs = list(getattr(self, "_history", []))
        if before is not None:
            bid = getattr(before, "id", before)
            msgs = [m for m in msgs if m.id < bid]
        # Discord returns newest first.
        for m in sorted(msgs, key=lambda m: m.id, reverse=True)[:limit]:
            yield m

    async def send(self, content, **kwargs):
        self.sent.append(content)
        return types.SimpleNamespace(id=1)


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
        assert "setInterval(poll" in body                         # live refresh


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
        assert f"discord.com/channels/{guild.id}/10/1001" in body   # jump link
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


def test_html_escaping_blocks_injection():
    assert esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


async def _noop():
    return None
