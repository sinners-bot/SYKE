from datetime import datetime, timezone

from syke.cards import archetype, aura_page, build_pages, iq_page
from syke.demo import build_demo_server
from syke.profile import analyze_server, build_profile
from syke.roast import fallback_summary
from syke.verdicts import aura_blurb, iq_blurb, persona

from test_commands import FakeCtx, run

UTC = timezone.utc
BASE = datetime(2026, 6, 1, 14, tzinfo=UTC)


def demo_users():
    return analyze_server(build_demo_server(), UTC, min_messages=15)


def test_every_demo_member_gets_a_different_writeup():
    server = demo_users()
    titles, iq_texts, aura_texts, roasts = {}, {}, {}, {}
    for uid, user in server.users.items():
        profile = build_profile(server, uid)
        titles[user.name] = persona(profile)[0]
        iq_texts[user.name] = iq_blurb(user.name, user.iq)
        aura_texts[user.name] = aura_blurb(user.name, user.aura)
        roasts[user.name] = fallback_summary(profile, rng=__import__("random").Random(uid))
        assert user.name in iq_texts[user.name]
        assert user.name in aura_texts[user.name]
        assert titles[user.name] in roasts[user.name]
    assert len(set(titles.values())) >= 4
    assert len(set(iq_texts.values())) == len(server.users)
    assert len(set(aura_texts.values())) == len(server.users)
    assert len(set(roasts.values())) == len(server.users)
    assert archetype(build_profile(server, 1))[0] == titles["Zyro"]


def test_iq_and_aura_cards_use_the_personal_blurb():
    server = demo_users()
    mira, dex = server.users[2], server.users[4]
    iq = iq_page(mira.name, mira.iq, 30)
    aura = aura_page(dex.name, dex.aura, 40)
    assert mira.name in iq.description and mira.iq.label in iq.description
    assert dex.name in aura.description and dex.aura.label in aura.description
    assert iq.description != aura_page(mira.name, server.users[3].aura, None).description


def test_profile_pages_use_the_same_persona():
    profile = build_profile(demo_users(), 1)
    title, _ = archetype(profile)
    pages = build_pages(profile)
    assert pages["overview"].description.startswith(f"**{title}**")
    assert pages["report"].description.startswith(f"**{title}**")


def test_aura_reads_stored_messages_without_a_live_scan(cog):
    ctx = FakeCtx()
    ctx.author.display_avatar = type("A", (), {"url": "http://a"})()
    cog.bot.storage.track(42, 1)
    stamp = BASE.timestamp()
    cog.bot.storage.add_corpus(42, [
        (i, 1, 1234, "yeah we can go later", stamp + i, "Tester", 0, 0) for i in range(20)
    ])
    run(cog.aura.callback(cog, ctx, None))
    card = ctx.replies[-1]
    assert card.title == "Tester's Aura"
    assert "Tester" in card.description
    assert cog.bot.scanner.cached_at(42) is None
