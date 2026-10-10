from datetime import datetime, timedelta, timezone

from syke.aura import AURA_MAX, AURA_MIN, bell_curve, compute_aura
from syke.cards import aura_page, build_pages
from syke.demo import build_demo_server
from syke.models import Msg
from syke.profile import analyze_server, build_profile
from syke.stats import compute_stats
from syke.traits import score_user

from test_commands import FakeCtx, run

UTC = timezone.utc
BASE = datetime(2026, 6, 1, 14, tzinfo=UTC)


def aura_of(messages):
    stats = compute_stats(messages, UTC)
    scores, _ = score_user(messages, UTC)
    return compute_aura(messages, stats, scores)


def test_dry_texters_beat_cringe():
    dry = aura_of([Msg(1, "x", t, BASE + timedelta(minutes=i)) for i, t in enumerate(
        ["ok", "yeah", "true", "gg", "sure", "later", "w", "nice"] * 6
    )])
    cringe = aura_of([Msg(1, "x", t, BASE + timedelta(minutes=i)) for i, t in enumerate([
        "uwu hiii bestie :3 slay periodt", "skibidi rizz ohio 💀", "rawr x3 nuzzles",
        "omg slay periodt ✨✨💅", "teehee >w< smol bean",
    ] * 6)])
    yap = aura_of([Msg(1, "x", t, BASE + timedelta(minutes=i)) for i, t in enumerate([
        "Honestly I think the entire historical context needs to be considered before anyone replies.",
        "That interpretation is plausible although the statistics are questionable and honestly overstated.",
    ] * 10)])
    assert dry.aura > 115 > 90 > cringe.aura
    assert dry.aura > yap.aura
    assert AURA_MIN <= cringe.aura <= dry.aura <= AURA_MAX
    assert dry.highest is not None and dry.lowest is not None
    assert dry.aura == 100 + sum(f.points for f in dry.factors) or dry.aura in (AURA_MIN, AURA_MAX)


def test_aura_bell_curve_marker_moves():
    low, mid, high = (bell_curve(v).splitlines()[1].index("▲") for v in (10, 100, 180))
    assert low < mid < high


def test_aura_on_profile_and_ranks():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    profile = build_profile(server, 4)
    assert profile.aura and profile.server_ranks["aura"] >= 1
    pages = build_pages(profile)
    assert f"✨ Aura {profile.aura.aura}" in pages["overview"].description
    assert any(f.name == "✨ Aura" for f in pages["report"].fields)
    assert "✨ Highest aura" in pages["ranks"].fields[-1].value
    dex = server.users[4].aura.aura
    bubbles = server.users[3].aura.aura
    assert dex > bubbles


def test_aura_card_layout():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    result = server.users[4].aura
    card = aura_page("Dex", result, 40)
    assert card.title == "Dex's Aura"
    assert card.description.startswith(f"## {result.aura}\n**{result.label}**")
    assert "More aura than **40%**" in card.description
    names = [f.name for f in card.fields]
    assert names[0] == "🔬 How SYKE measured"
    assert "✨ Aura farm" in names and "📉 Aura loss" in names
    assert len(card) < 2000


def test_aura_and_top_aura_commands(cog):
    ctx = FakeCtx()
    ctx.author.display_avatar = type("A", (), {"url": "http://a"})()
    run(cog.aura.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "No channels are being watched"

    run(cog.demo.callback(cog, ctx, True))
    run(cog.top.callback(cog, ctx, "aura"))
    board = ctx.replies[-1]
    assert board.title == "✨ Highest aura — server leaderboard"
    assert "Aura " in board.description

    run(cog.aura.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "Insufficient evidence 🔬"
