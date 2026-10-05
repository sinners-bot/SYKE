import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from syke.ai import _apply_ai_result, write_roast
from syke.config import load_settings
from syke.models import Msg
from syke.demo import build_demo_server
from syke.profile import analyze_server, build_profile
from syke.render import render_report
from syke.stats import compute_stats, peak_window
from syke.traits import score_user

UTC = timezone.utc
BASE = datetime(2026, 6, 1, tzinfo=UTC)


def msg(text: str, minutes: int = 0, author: int = 1, hour: int = 12, laughs: int = 0) -> Msg:
    when = BASE.replace(hour=hour) + timedelta(minutes=minutes)
    return Msg(author, f"user{author}", text, when, laugh_reactions=laughs)


def test_peak_window_wraps_midnight():
    hours = [0] * 24
    hours[23] = hours[0] = hours[1] = 10
    assert peak_window(hours) == (23, 2)


def test_stats_counts_emojis_and_phrases():
    msgs = [msg("bro really thought 💀", i) for i in range(4)] + [msg("<:pepe:123> ok", 9)]
    st = compute_stats(msgs, UTC)
    assert st.message_count == 5
    assert st.top_emojis[0] == ("💀", 4)
    assert ("<:pepe:123>", 1) in st.top_emojis
    assert st.top_phrases == [("bro really thought", 4)]


def test_traits_separate_personalities():
    chaotic, _ = score_user([msg("ASDFGHJKL WHY!!!", i * 60, hour=2) for i in range(20)], UTC)
    calm, _ = score_user(
        [msg("Honestly I think the evidence points the other way because of context.", i * 60) for i in range(20)],
        UTC,
    )
    assert chaotic["chaotic"] > 70 > calm["chaotic"]
    assert calm["serious"] > 60 > chaotic["serious"]


def test_laugh_reactions_make_you_funny():
    plain, _ = score_user([msg("i went outside today", i) for i in range(20)], UTC)
    loved, _ = score_user([msg("i went outside today", i, laughs=3) for i in range(20)], UTC)
    assert loved["funny"] > plain["funny"] + 50


def test_min_messages_threshold():
    server = analyze_server([msg("hi", i, author=7) for i in range(3)], UTC, min_messages=5)
    assert server.users == {}
    assert server.below_threshold == {7: 3}


def test_full_report_renders_offline():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    settings = replace(load_settings(), ai_provider="none")
    profile = asyncio.run(write_roast(build_profile(server, 1), settings))
    report = render_report(profile)
    for section in ("ACTIVITY", "PERSONALITY BREAKDOWN", "PROFILE SUMMARY", "ACHIEVEMENTS", "Compared with the server"):
        assert section in report
    assert "Zyro" in profile.summary
    assert not profile.ai_used
    assert "```" not in report
    assert all(len(line) <= 40 for line in report.splitlines())


def test_solo_user_ranked_against_demo_members():
    me = [msg("LMAOOO bro 💀 i cant", i * 30, author=987654321098765432, laughs=1) for i in range(15)]
    server = analyze_server(me + build_demo_server(headline_volume=400), UTC, min_messages=15)
    profile = build_profile(server, 987654321098765432)
    assert profile.server_size == 6
    assert "Top" in render_report(profile)


def test_storage_migrates_old_guild_settings(tmp_path):
    import sqlite3

    from syke.storage import Storage

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, timezone TEXT)")
    conn.execute("INSERT INTO guild_settings VALUES (1, 'Europe/London')")
    conn.commit()
    conn.close()

    store = Storage(str(db))
    assert store.timezone(1) == "Europe/London" and not store.demo_mode(1)
    store.set_demo_mode(1, True)
    assert store.demo_mode(1) and store.timezone(1) == "Europe/London"
    store.set_timezone(2, "UTC")
    assert not store.demo_mode(2)


def test_ai_result_only_accepts_known_messages():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    profile = build_profile(server, 5)
    sample = [replace(m, message_id=100 + i) for i, m in enumerate(profile.messages[:5])]
    _apply_ai_result(profile, {
        "summary": "Vex is a menace :salt:.", "toxic": 2, "funniest": 999,
        "bonus_achievement": {"emoji": "🧂", "name": "Salt Mine"},
        "labels": [{"i": 1, "traits": ["toxic", "nonsense"]}, {"i": 50, "traits": ["funny"]}, "junk"],
        "trait_scores": {"toxic": 140, "funny": 12, "made_up": 5},
    }, sample, {"salt": "<:salt:42>"})
    assert profile.ai_used and profile.summary == "Vex is a menace <:salt:42>."
    assert profile.highlights["toxic"] is sample[2]
    assert ("🧂", "Salt Mine") in profile.achievements
    assert profile.ai_labels == {101: frozenset({"toxic"})}
    assert profile.ai_scores == {"toxic": 100, "funny": 12}


def test_ai_labels_raise_scores_and_ranks():
    from syke.profile import refine_scores

    msgs = [replace(msg("ok see you later then", i, author=1), message_id=i + 1) for i in range(20)]
    msgs += [msg("lmao 💀 i cant", i, author=2) for i in range(20)]
    server = analyze_server(msgs, UTC, min_messages=5)
    profile = build_profile(server, 1)
    assert profile.scores["funny"] < server.users[2].scores["funny"]
    labels = {i + 1: frozenset({"funny"}) for i in range(20)}
    profile.ai_scores = {"funny": 100}
    refine_scores(profile, server, UTC, labels)
    assert profile.scores["funny"] > server.users[2].scores["funny"]
    assert profile.server_ranks["funny"] == 50


def test_prompt_shows_emoji_names_and_style():
    from syke.ai import _sample_messages, _user_prompt

    msgs = [msg("<:KEKW:12> bro", i) for i in range(20)]
    profile = build_profile(analyze_server(msgs, UTC, min_messages=5), 1)
    prompt = _user_prompt(profile, _sample_messages(profile), "a police report", ["KEKW"], "old roast")
    assert ":KEKW: bro" in prompt and "<:KEKW:12>" not in prompt
    assert "a police report" in prompt and "SERVER EMOJIS YOU MAY USE: :KEKW:" in prompt
    assert "old roast" in prompt


def test_offline_summaries_vary_and_use_real_messages():
    import random

    from syke.roast import fallback_summary

    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    profile = build_profile(server, 1)
    summaries = {fallback_summary(profile, random.Random(seed)) for seed in range(12)}
    assert len(summaries) >= 10
    assert all(s.startswith("Zyro") or "Zyro" in s.split("\n")[0] for s in summaries)
    quotes = {m.content for m in profile.messages}
    assert any(any(q[:20] in s for q in quotes) for s in summaries)
    with_emoji = fallback_summary(profile, random.Random(1), {"KEKW": "<:KEKW:9>", "cat": "<:cat:8>"})
    assert with_emoji.endswith("<:KEKW:9>")


def test_custom_emoji_names_and_phrases_count():
    from syke.traits import score_message

    plain = score_message(msg("ok"), UTC)
    assert score_message(msg("<:KEKW:1>"), UTC)["funny"] > plain["funny"]
    assert score_message(msg("<:pepe_horny:2>"), UTC)["freaky"] > plain["freaky"]
    assert score_message(msg("nobody asked, cry about it"), UTC)["toxic"] > 1
    assert score_message(msg("To be fair, it depends on the context."), UTC)["serious"] > 0.6
    assert score_message(msg("ok"), UTC, ai_labels=frozenset({"cringe"}))["cringe"] > 1


def test_user_text_cannot_escape_code_block():
    msgs = [msg("lmao ```@everyone``` 💀", i, laughs=2) for i in range(20)]
    server = analyze_server(msgs, UTC, min_messages=5)
    profile = build_profile(server, 1)
    profile.summary = "x ``` y"
    assert "```" not in render_report(profile)
