from datetime import date, datetime, timedelta, timezone

from syke.cards import build_pages, iq_page, short_summary
from syke.demo import build_demo_server
from syke.iq import IQ_MAX, IQ_MIN, bell_curve, compute_iq
from syke.models import Msg
from syke.profile import analyze_server, build_profile
from syke.stats import compute_stats
from syke.traits import score_user

from test_commands import FakeCtx, run

UTC = timezone.utc
BASE = datetime(2026, 6, 1, 14, tzinfo=UTC)


def msgs(text: str, n: int = 30) -> list[Msg]:
    return [Msg(1, "x", f"{text} {i}", BASE + timedelta(minutes=i)) for i in range(n)]


def iq_of(messages):
    stats = compute_stats(messages, UTC)
    scores, _ = score_user(messages, UTC)
    return compute_iq(messages, stats, scores)


def test_careful_writers_beat_brainrot():
    thoughtful = iq_of([Msg(1, "x", t, BASE + timedelta(minutes=i)) for i, t in enumerate([
        "Honestly, the economic argument ignores the historical context entirely.",
        "I disagree, because the evidence suggests a fundamentally different conclusion.",
        "Realistically, governments prioritise infrastructure investment over education.",
        "That interpretation is plausible, although the statistics are questionable.",
    ] * 8)])
    brainrot = iq_of([Msg(1, "x", t, BASE + timedelta(minutes=i)) for i, t in enumerate([
        "skibidi rizz ohio 💀", "lol", "LMAOOO", "bruh", "fr fr no cap", "sigma gyatt",
    ] * 6)])
    assert thoughtful.iq > 115 > 85 > brainrot.iq
    assert IQ_MIN <= brainrot.iq and thoughtful.iq <= IQ_MAX
    assert thoughtful.smartest is not None and thoughtful.dumbest is not None
    assert thoughtful.iq == 100 + sum(f.points for f in thoughtful.factors) or thoughtful.iq == IQ_MAX


def test_bell_curve_marker_moves():
    low, mid, high = (bell_curve(v).splitlines()[1].index("▲") for v in (60, 100, 150))
    assert low < mid < high


def test_iq_on_profile_and_ranks():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    profile = build_profile(server, 2)
    assert profile.iq and profile.server_ranks["iq"] >= 1
    pages = build_pages(profile)
    assert f"🧠 IQ {profile.iq.iq}" in pages["overview"].description
    assert any(f.name == "🧠 IQ" for f in pages["report"].fields)
    assert "🎓 Highest IQ" in pages["ranks"].fields[-1].value
    mira = server.users[2].iq.iq
    assert mira == max(u.iq.iq for u in server.users.values())


def test_iq_card_layout():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    result = server.users[3].iq
    card = iq_page("bubbles", result, 25)
    assert card.title == "bubbles's IQ"
    assert card.description.startswith(f"## {result.iq}\n**{result.label}**")
    assert "Smarter than **25%**" in card.description
    names = [f.name for f in card.fields]
    assert names == ["🔬 How SYKE measured", "🎓 Smartest message", "🤡 Least smart message"]
    assert len(card) < 2000


def test_iq_and_top_iq_commands(cog):
    ctx = FakeCtx()
    ctx.author.display_avatar = type("A", (), {"url": "http://a"})()
    run(cog.iq.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "No channels are being watched"

    run(cog.demo.callback(cog, ctx, True))
    run(cog.top.callback(cog, ctx, "iq"))
    board = ctx.replies[-1]
    assert board.title == "🧠 Highest IQ — server leaderboard"
    assert "Mira" in board.description.splitlines()[1] and "IQ " in board.description

    run(cog.iq.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "Insufficient evidence 🔬"


def test_peak_hours_render_in_each_readers_timezone():
    stats = compute_stats([Msg(1, "x", "hi", BASE + timedelta(minutes=i)) for i in range(5)], UTC)
    assert stats.peak_label == "13:00–16:00 UTC"
    local = stats.peak_local(date(2026, 6, 1))
    start = int(datetime(2026, 6, 1, 13, tzinfo=UTC).timestamp())
    assert local == f"<t:{start}:t>–<t:{start + 3 * 3600}:t>"

    late = compute_stats([Msg(1, "x", "hi", datetime(2026, 6, 1, 23, tzinfo=UTC)) for _ in range(3)], UTC)
    s, e = (int(x[3:-3]) for x in late.peak_local(date(2026, 6, 1)).split("–"))
    assert e - s == 3 * 3600


def test_short_summary_never_splits_a_timestamp():
    text = "a" * 240 + " <t:1780322400:t>–<t:1780333200:t> more words here"
    cut = short_summary(text, limit=260)
    assert "<t:" not in cut or cut.count("<") == cut.count(">")
