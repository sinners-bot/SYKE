"""THE STRIP feed: highlights posted only where a server opted in, rate-limited, with a Play button; the live count."""
import asyncio
import time
import types

import pytest

discord = pytest.importorskip("discord")
if not getattr(discord, "__file__", None):   # the do-anything stand-in other Strip tests install: needs the real discord.py
    pytest.skip("needs discord.py", allow_module_level=True)
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
                                       ("newplayer", {}), ("jackpot", {"game": "slots", "net": 9000, "jackpot": "Mega"}),
                                       ("pet", {"pet": "Biscuit", "breed": "Shiba Inu"})])
def test_every_highlight_reads_well(kind, data):
    f, *_ = _setup()
    e = f.highlight_embed(kind, {"player": {"name": "Dee"}, "data": data})
    assert "Dee" in e.description and "{" not in e.description


def test_showcase_pages_have_screenshots_and_a_play_link():
    f, *_ = _setup()
    f.app_id, f.online = "123456", 9
    for i, (key, _, _) in enumerate(F.SHOWCASE):
        e = f.play_embed(None, i)
        assert e.image.url == "attachment://strip.jpg" and f"{i + 1}/{len(F.SHOWCASE)}" in e.footer.text
        assert (F.ASSETS / f"{key}.jpg").exists()
    assert any("9" in fld.value for fld in f.play_embed(None, 0).fields)
    v = F.ShowcaseView(f, 0)
    assert any(getattr(c, "url", None) == "https://discord.com/activities/123456" for c in v.children)


def test_live_post_links_the_voice_channel_and_can_be_muted():
    """3.25: "Ava is playing THE STRIP in 🔊 Lounge (3 in the call)", only where opted in, and !striplive off mutes it."""
    f, posted, pres, cfg = _setup()
    class FakeVC(discord.VoiceChannel):   # a voice channel with three people in it (no gateway needed)
        def __init__(self):
            pass
        mention = property(lambda self: "<#42>")
        members = property(lambda self: [1, 2, 3])

    vc = FakeVC()
    orig = f.bot.get_guild

    def guild_with_vc(g):
        gd = orig(g)
        if gd is None:
            return None
        base = gd.get_channel
        gd.get_channel = lambda i: vc if i == 42 else base(i)
        return gd

    f.bot.get_guild = guild_with_vc

    async def go():
        cfg[77] = {"channel": 5}
        r = await f.handle({"kind": "live", "guild_id": "77", "player": {"name": "Ava"}, "data": {"channel": "42", "here": 1}})
        assert r["posted"]
        e, v = posted[-1]
        assert "Ava" in e.description and "<#42>" in e.description and "3 in the call" in e.description
        f._sent.clear()
        r = await f.handle({"kind": "live", "guild_id": "77", "player": {"name": "Ben"}, "data": {"channel": "999"}})
        assert r["posted"] and "right now" in posted[-1][0].description
        f._sent.clear()
        cfg[77] = {"channel": 5, "mute": ["live"]}
        assert (await f.handle({"kind": "live", "guild_id": "77", "player": {"name": "Cy"}, "data": {"channel": "42"}}))["why"] == "muted_kind"

    asyncio.run(go())
