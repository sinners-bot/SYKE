"""THE STRIP feed: highlights posted only where a server opted in, rate-limited, with a Play button; the live count."""
import asyncio
import time
import types

import pytest

discord = pytest.importorskip("discord")
from syke import strip_feed as F  # noqa: E402


class _Perms:
    send_messages = True
    embed_links = True


def _setup():
    posted, pres, cfg = [], [], {}

    class Chan:
        def permissions_for(self, m):
            return _Perms()

        async def send(self, embed=None, view=None, allowed_mentions=None):
            posted.append((embed, view))

    class Guild:
        me = object()

        def get_channel(self, i):
            return Chan() if i == 5 else None

    class Bot:
        settings = types.SimpleNamespace(default_prefix="!")
        storage = types.SimpleNamespace(get_config=lambda g, k: cfg.get(g, {}))

        def get_guild(self, g):
            return Guild() if g == 77 else None

        async def change_presence(self, activity):
            pres.append(activity)

    return F.StripFeed(Bot()), posted, pres, cfg


def test_feed_posts_only_where_opted_in_and_rate_limits():
    f, posted, pres, cfg = _setup()

    async def go():
        assert (await f.handle({"kind": "online", "online": 12, "app_id": "123456"}))["ok"]
        assert "12 playing" in pres[-1].name
        r = await f.handle({"kind": "record", "guild_id": "77", "player": {"name": "Ana"}, "data": {"game": "Neon Rally", "score": "41.20s"}})
        assert r["why"] == "no_channel"
        cfg[77] = {"channel": 5}
        r = await f.handle({"kind": "record", "guild_id": "77", "player": {"name": "Ana"}, "data": {"game": "Neon Rally", "score": "41.20s"}})
        assert r["posted"]
        e, v = posted[-1]
        assert "Ana" in e.description and "Neon Rally" in e.description
        assert v.children[0].url == "https://discord.com/activities/123456"
        r = await f.handle({"kind": "jackpot", "guild_id": "77", "player": {"name": "Ben"}, "data": {"net": 50000}})
        assert r["why"] == "rate"
        f._sent[77] = [time.time() - 100 * i for i in range(25, 0, -1)]
        assert (await f.handle({"kind": "newplayer", "guild_id": "77", "player": {"name": "E"}}))["why"] == "rate"
        assert (await f.handle({"kind": "bogus", "guild_id": "77"}))["error"] == "bad_kind"
        assert (await f.handle({"kind": "record", "guild_id": "99"}))["why"] == "not_here"

    asyncio.run(go())


@pytest.mark.parametrize("kind,data", [("wedding", {"partner": "Cy"}), ("cup", {"game": "Beat Street"}),
                                       ("promotion", {"title": "Head Chef", "job": "Restaurant", "level": 4}),
                                       ("newplayer", {}), ("jackpot", {"game": "slots", "net": 9000, "jackpot": "Mega"})])
def test_every_highlight_reads_well(kind, data):
    f, *_ = _setup()
    e = f.highlight_embed(kind, {"player": {"name": "Dee"}, "data": data})
    assert "Dee" in e.description and "{" not in e.description
