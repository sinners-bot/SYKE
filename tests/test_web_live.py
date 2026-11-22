import asyncio
import gzip
import types
from datetime import datetime, timezone

from test_web import _session_cookie, dash, serving  # noqa: F401


def author(uid=7, name="Ada"):
    return types.SimpleNamespace(id=uid, display_name=name, bot=False, display_avatar=types.SimpleNamespace(url="a"),
                                 colour=types.SimpleNamespace(value=0), created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))


def message(guild, mid, text, channel_id=10, reactions=None, edited=False):
    ch = guild.get_channel(channel_id)
    return types.SimpleNamespace(
        id=mid, guild=guild, channel=ch, author=author(), content=text, clean_content=text,
        created_at=datetime(2026, 11, 22, tzinfo=timezone.utc), reference=None, attachments=[], embeds=[],
        stickers=[], reactions=reactions or [], edited_at=datetime(2026, 11, 22, tzinfo=timezone.utc) if edited else None)


async def recv(ws):
    return await asyncio.wait_for(ws.receive_json(), timeout=2)


async def test_feed_streams_messages_edits_deletes_and_reactions(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        ws = await cl.ws_connect(f"/g/{guild.id}/live/ws")
        everything = await cl.ws_connect("/live/ws")
        msg = message(guild, 500, "hello world")
        await web.live.on_message(msg)
        got = await recv(ws)
        assert got["t"] == "feed" and got["k"] == "msg" and "hello world" in got["html"] and "#general" in got["html"]
        assert (await recv(everything))["k"] == "msg"                     # the all-servers feed gets it too

        edited = message(guild, 500, "hello there", edited=True)
        await web.live.on_raw_message_edit(types.SimpleNamespace(
            guild_id=guild.id, channel_id=10, message_id=500, message=edited, cached_message=None))
        got = await recv(ws)
        assert got["k"] == "edit" and "hello world" in got["html"] and "hello there" in got["html"]

        await web.live.on_raw_reaction_add(types.SimpleNamespace(
            guild_id=guild.id, channel_id=10, message_id=500, user_id=7, member=None, emoji="😂"))
        got = await recv(ws)
        assert got["k"] == "rx" and "😂" in got["html"] and "Ada" in got["html"]

        await web.live.on_raw_message_delete(types.SimpleNamespace(guild_id=guild.id, channel_id=10, message_id=500))
        got = await recv(ws)
        assert got["k"] == "del" and "hello there" in got["html"]          # SYKE remembers what it said
        await ws.close()
        await everything.close()


async def test_feed_joins_and_voice(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        ws = await cl.ws_connect(f"/g/{guild.id}/live/ws")
        member = author(8, "Bo")
        member.guild = guild
        await web.live.on_member_join(member)
        got = await recv(ws)
        assert got["k"] == "join" and "Bo" in got["html"] and "days old" in got["html"]
        lounge = types.SimpleNamespace(name="Lounge")
        await web.live.on_voice_state_update(member, types.SimpleNamespace(channel=None),
                                             types.SimpleNamespace(channel=lounge, self_stream=False))
        got = await recv(ws)
        assert got["k"] == "voice" and "joined" in got["html"] and "Lounge" in got["html"]
        await ws.close()


async def test_chat_view_gets_live_reaction_bars_and_edit_text(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        ws = await cl.ws_connect(f"/g/{guild.id}/c/10/ws")
        rx = types.SimpleNamespace(emoji="🔥", count=3, me=False)
        target = message(guild, 600, "rate this", reactions=[rx])
        guild.get_channel(10)._history = [target]
        web.live.remember(message(guild, 600, "rate this"))
        await web.live.on_raw_reaction_add(types.SimpleNamespace(
            guild_id=guild.id, channel_id=10, message_id=600, user_id=7, member=None, emoji="🔥"))
        got = await recv(ws)
        assert got["t"] == "rx" and got["mid"] == "600" and "🔥 3" in got["html"]
        await web.live.on_raw_message_edit(types.SimpleNamespace(
            guild_id=guild.id, channel_id=10, message_id=600, message=message(guild, 600, "rate it", edited=True),
            cached_message=None))
        got = await recv(ws)
        assert got["t"] == "edit" and got["before"] == "rate this" and "(edited)" in got["html"]
        await ws.close()


async def test_embed_loads_are_not_edits(dash):
    web, bot, guild = dash
    sent = []

    async def spy(cid, payload):
        sent.append(payload)
    web._broadcast = spy
    web._subs[10].add(object())
    web.live.remember(message(guild, 700, "same text"))
    await web.live.on_raw_message_edit(types.SimpleNamespace(
        guild_id=guild.id, channel_id=10, message_id=700, message=message(guild, 700, "same text"),
        cached_message=None))
    assert sent == []


def test_latest_page_comes_from_the_message_cache(dash):
    web, bot, guild = dash
    from syke.web import MESSAGES_SHOWN
    msgs = [message(guild, 1000 + i, f"m{i}") for i in range(MESSAGES_SHOWN + 5)]
    bot._connection._messages = None
    type(bot).cached_messages = property(lambda self: msgs)
    try:
        page = web._from_cache(10)
        assert [m.id for m in page] == [m.id for m in msgs[-MESSAGES_SHOWN:]]
        assert web._from_cache(11) is None                                 # not enough cached for that channel
    finally:
        del type(bot).cached_messages


async def test_poll_fetches_only_newer_messages(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        ch = guild.get_channel(10)
        ch._history = [message(guild, i, f"msg {i}") for i in (1, 2, 3)]

        async def history(limit=50, before=None, around=None, after=None):
            for m in sorted(ch._history, key=lambda m: m.id):
                if after is None or m.id > after.id:
                    yield m
        ch.history = history
        text = await (await cl.get(f"/g/{guild.id}/c/10/messages?after=2")).text()
        assert "msg 3" in text and "msg 1" not in text and "msg 2" not in text


async def test_live_and_server_pages_render_and_compress(dash):
    async with serving(dash) as (cl, web, bot, guild):
        cl.session.cookie_jar.update_cookies(_session_cookie())
        web.live.remember(message(guild, 900, "earlier chat"))
        web.live.note_activity(message(guild, 900, "earlier chat"))
        resp = await cl.get(f"/g/{guild.id}/live", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
        assert resp.status == 200 and resp.headers.get("Content-Encoding") == "gzip"
        page = gzip.decompress(await resp.read()).decode()
        assert "earlier chat" in page and "All channels" in page and "live-kinds" in page   # backfilled
        page = await (await cl.get("/live")).text()
        assert "All servers" in page
        page = await (await cl.get(f"/g/{guild.id}")).text()
        assert "Busiest channels" in page and "Voice right now" in page and "1 today" in page
        home = await (await cl.get("/")).text()
        assert "1 msgs/hr" in home and "watch everything live" in home


async def test_live_requires_login(dash):
    async with serving(dash) as (cl, web, bot, guild):
        assert (await cl.get("/live", allow_redirects=False)).status == 302
        assert (await cl.get(f"/g/{guild.id}/live/ws")).status == 401
