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
        self._read = read
        self._send = send
        self.sent = []

    def permissions_for(self, who):
        return discord.Permissions(read_message_history=self._read, send_messages=self._send, view_channel=True)

    async def history(self, limit=50):
        for m in []:
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
        self.members = {7: Member(7)}
        self.banned = []
        self.left = False

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

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


def test_html_escaping_blocks_injection():
    assert esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


async def _noop():
    return None
